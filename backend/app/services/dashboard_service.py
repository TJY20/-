"""Dashboard 业务逻辑：KPI / 区域排行 / 风险站点 / 最近告警 / ETL 健康汇总."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.models.alert import Alert
from app.models.cur_station_status import CurStationStatus
from app.models.data_source import DataSource
from app.models.fact_station_snapshot import FactStationSnapshot
from app.models.fetch_log import FetchLog
from app.models.gbfs_feed import GbfsFeed
from app.models.region import Region
from app.models.station import Station
from app.schemas.dashboard import (
    AlertCountByLevel,
    DashboardKPI,
    DashboardSummary,
    DashboardTrend24h,
    EtlFeedHealth,
    OperationalAreaRankingItem,
    RecentAlertItem,
    RegionRankingItem,
    RisksResponse,
    RiskStationItem,
    StationAlertItem,
    StationAlertsResponse,
    StationDetail,
    StationGeoFeature,
    StationGeoFeatureCollection,
    StationGeometry,
    StationGeoProperties,
    StationHistoryPoint,
    StationHistoryResponse,
    TrendBucket,
    TrendRegionPoint,
    TrendRegionSeries,
)


_EMPTY_BIKES_THRESHOLD = 1  # 可借车 ≤ 1 视为空车风险
_FULL_OCCUPANCY_THRESHOLD = 0.95  # 占用率 ≥ 95% 视为满桩风险
_FULL_DOCKS_THRESHOLD = 1  # 可还桩 ≤ 1 视为满桩风险（docks_available 来自 GBFS）


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# 数据源切换：真实优先，Mock 兜底
# ---------------------------------------------------------------------------


def _get_active_source_filter(session: Session) -> str | None:
    """判断当前应优先展示的数据源.

    若历史上有过非-mock 的 fetch_log success，则返回最近一次的 source_code，
    dashboard 查询时仅展示该 source 的站点（即使数据已过期也不混 mock）。
    否则返回 None，展示全部（含 mock）。
    """
    latest = session.exec(
        select(FetchLog, DataSource)
        .join(GbfsFeed, FetchLog.feed_id == GbfsFeed.id)
        .join(DataSource, GbfsFeed.source_id == DataSource.id)
        .where(
            FetchLog.status == "success",
            DataSource.code != "CN-MOCK",
        )
        .order_by(FetchLog.started_at.desc())
        .limit(1)
    ).first()
    if latest:
        _, source = latest
        station_count = session.exec(
            select(func.count(Station.id)).where(Station.source_system == source.code)
        ).one()
        if station_count:
            return source.code
    return None


def _alerts_count_by_level(
    session: Session, mode: str = "real"
) -> AlertCountByLevel:
    """按级别统计未处理告警数量（含动态风险告警）.

    mode="real": 统计当前活跃真实数据源的告警 + 动态风险告警；
    mode="mock": 只统计 Mock 演示告警。
    """
    # 复用 get_recent_alerts 的合并逻辑（alert 表 + 动态风险告警），取大 limit 保证全量
    alerts = get_recent_alerts(session, limit=99999, mode=mode)
    bucket = {"info": 0, "warning": 0, "critical": 0}
    for a in alerts:
        if a.level in bucket:
            bucket[a.level] += 1
    return AlertCountByLevel(**bucket)


def get_kpi(session: Session) -> DashboardKPI:
    active_source = _get_active_source_filter(session)
    station_filter = (
        Station.source_system == active_source
        if active_source
        else Station.source_system == "mock"
    )

    total_stations = session.exec(
        select(func.count(Station.id)).where(station_filter)
    ).one()
    active_stations = session.exec(
        select(func.count(Station.id)).where(
            Station.status == "active", station_filter
        )
    ).one()

    if active_source:
        # 按 source 过滤时需 join station
        bikes_sum = session.exec(
            select(func.coalesce(func.sum(CurStationStatus.bikes_available), 0))
            .join(Station, Station.id == CurStationStatus.station_id)
            .where(station_filter)
        ).one()
        docks_sum = session.exec(
            select(func.coalesce(func.sum(CurStationStatus.docks_available), 0))
            .join(Station, Station.id == CurStationStatus.station_id)
            .where(station_filter)
        ).one()
        avg_occ = session.exec(
            select(func.coalesce(func.avg(CurStationStatus.occupancy_rate), 0.0))
            .join(Station, Station.id == CurStationStatus.station_id)
            .where(station_filter)
        ).one()
    else:
        bikes_sum = session.exec(
            select(func.coalesce(func.sum(CurStationStatus.bikes_available), 0))
        ).one()
        docks_sum = session.exec(
            select(func.coalesce(func.sum(CurStationStatus.docks_available), 0))
        ).one()
        avg_occ = session.exec(
            select(func.coalesce(func.avg(CurStationStatus.occupancy_rate), 0.0))
        ).one()

    # 系统最近一次成功抓取时间（fetch_log）
    latest_success_log = session.exec(
        select(FetchLog)
        .join(GbfsFeed, FetchLog.feed_id == GbfsFeed.id)
        .join(DataSource, GbfsFeed.source_id == DataSource.id)
        .where(FetchLog.status == "success")
        .order_by(FetchLog.finished_at.desc())
        .limit(1)
    ).first()
    system_updated_at: datetime | None = None
    if latest_success_log and latest_success_log.finished_at:
        system_updated_at = latest_success_log.finished_at

    # GBFS 官方最近上报时间（cur_station_status.last_reported）
    if active_source:
        source_reported_at = session.exec(
            select(func.max(CurStationStatus.last_reported))
            .join(Station, Station.id == CurStationStatus.station_id)
            .where(station_filter)
        ).one()
    else:
        source_reported_at = session.exec(
            select(func.max(CurStationStatus.last_reported))
        ).one()

    return DashboardKPI(
        total_stations=int(total_stations or 0),
        active_stations=int(active_stations or 0),
        total_bikes_available=int(bikes_sum or 0),
        total_docks_available=int(docks_sum or 0),
        avg_occupancy_rate=float(round(avg_occ or 0.0, 4)),
        alerts=_alerts_count_by_level(session),
        system_updated_at=system_updated_at,
        source_reported_at=source_reported_at,
        last_updated=system_updated_at,
    )


def get_region_ranking(session: Session, limit: int = 5) -> list[RegionRankingItem]:
    """按区域汇总：站点数、可用车总数、平均占用率、未处理告警数."""
    active_source = _get_active_source_filter(session)
    region_stats: list[RegionRankingItem] = []
    regions = session.exec(select(Region)).all()

    for region in regions:
        stmt = select(Station).where(Station.region_id == region.id)
        if active_source:
            stmt = stmt.where(Station.source_system == active_source)
        else:
            stmt = stmt.where(Station.source_system == "mock")
        stations = session.exec(stmt).all()
        if not stations:
            continue
        station_ids = [s.id for s in stations if s.id is not None]

        bikes_total = session.exec(
            select(func.coalesce(func.sum(CurStationStatus.bikes_available), 0))
            .where(CurStationStatus.station_id.in_(station_ids))
        ).one() if station_ids else 0
        avg_occ = session.exec(
            select(func.coalesce(func.avg(CurStationStatus.occupancy_rate), 0.0))
            .where(CurStationStatus.station_id.in_(station_ids))
        ).one() if station_ids else 0.0
        open_alerts = session.exec(
            select(func.count(Alert.id))
            .where(Alert.station_id.in_(station_ids), Alert.status == "open")
        ).one() if station_ids else 0

        region_stats.append(
            RegionRankingItem(
                region_id=region.id or 0,
                code=region.code,
                name=region.name,
                city=region.city,
                station_count=len(stations),
                bikes_total=int(bikes_total or 0),
                avg_occupancy=float(round(avg_occ or 0.0, 4)),
                open_alerts=int(open_alerts or 0),
            )
        )

    region_stats.sort(key=lambda r: (r.open_alerts, -r.bikes_total), reverse=True)
    return region_stats[:limit]


_GRID_STEP = 0.02  # 约 2km x 2km（NYC 纬度）


def get_operational_area_ranking(
    session: Session, step: float = _GRID_STEP, limit: int = 10
) -> list[OperationalAreaRankingItem]:
    """基于经纬度网格划分的运营片区排行.

    将符合条件的站点按 lat/lng 做 grid bucketing，
    每个网格作为一个 operational_area 统计运营指标。
    """
    active_source = _get_active_source_filter(session)

    # 查询所有符合条件的 station + cur_status
    stmt = (
        select(Station, CurStationStatus)
        .join(CurStationStatus, CurStationStatus.station_id == Station.id, isouter=True)
    )
    if active_source:
        stmt = stmt.where(Station.source_system == active_source)
    else:
        stmt = stmt.where(Station.source_system == "mock")

    rows = session.exec(stmt).all()

    # grid bucketing
    grid: dict[str, dict[str, Any]] = {}
    for station, status in rows:
        if station.lat is None or station.lng is None:
            continue
        lat_b = (station.lat // step) * step
        lng_b = (station.lng // step) * step
        aid = f"{lat_b:.2f}_{lng_b:.2f}"

        cell = grid.setdefault(aid, {
            "lat_sum": 0.0,
            "lng_sum": 0.0,
            "station_count": 0,
            "bikes_total": 0,
            "docks_total": 0,
            "capacity_total": 0,
            "occupancy_sum": 0.0,
            "occupancy_count": 0,
        })
        cell["lat_sum"] += station.lat
        cell["lng_sum"] += station.lng
        cell["station_count"] += 1
        cell["capacity_total"] += int(station.capacity or 0)
        if status:
            cell["bikes_total"] += status.bikes_available
            cell["docks_total"] += status.docks_available
            cell["occupancy_sum"] += status.occupancy_rate
            cell["occupancy_count"] += 1

    items: list[OperationalAreaRankingItem] = []
    for aid, cell in grid.items():
        cnt = cell["station_count"]
        if cnt == 0:
            continue
        avg_occ = (
            cell["occupancy_sum"] / cell["occupancy_count"]
            if cell["occupancy_count"] > 0
            else 0.0
        )
        # 网格名称：基于中心坐标
        center_lat = cell["lat_sum"] / cnt
        center_lon = cell["lng_sum"] / cnt
        lat_dir = "N" if center_lat >= 0 else "S"
        lng_dir = "E" if center_lon >= 0 else "W"
        area_name = f"{abs(center_lat):.2f}°{lat_dir}, {abs(center_lon):.2f}°{lng_dir}"

        items.append(
            OperationalAreaRankingItem(
                area_id=aid,
                area_name=area_name,
                center_lat=round(center_lat, 4),
                center_lon=round(center_lon, 4),
                station_count=cnt,
                bikes_total=cell["bikes_total"],
                docks_total=cell["docks_total"],
                capacity_total=cell["capacity_total"],
                avg_occupancy=float(round(avg_occ, 4)),
            )
        )

    # 按站点数降序，站点数相同按车辆数降序
    items.sort(key=lambda x: (x.station_count, x.bikes_total), reverse=True)
    return items[:limit]


def _build_risk_item(
    session: Session, station: Station, status: CurStationStatus, risk_type: str
) -> RiskStationItem:
    region: Optional[Region] = (
        session.get(Region, station.region_id) if station.region_id else None
    )
    return RiskStationItem(
        station_id=station.id or 0,
        code=station.code,
        name=station.name,
        region_code=region.code if region else None,
        region_name=region.name if region else None,
        lat=station.lat,
        lng=station.lng,
        capacity=station.capacity,
        bikes_available=status.bikes_available,
        docks_available=status.docks_available,
        occupancy_rate=float(round(status.occupancy_rate, 4)),
        risk_type=risk_type,
    )


def get_risks(session: Session, limit_per_type: int = 10) -> RisksResponse:
    """空车风险（车少）+ 满桩风险（占用率高）."""
    active_source = _get_active_source_filter(session)

    empty_stmt = (
        select(Station, CurStationStatus)
        .join(CurStationStatus, CurStationStatus.station_id == Station.id)
        .where(CurStationStatus.bikes_available <= _EMPTY_BIKES_THRESHOLD)
        .order_by(CurStationStatus.bikes_available.asc())
        .limit(limit_per_type)
    )
    full_stmt = (
        select(Station, CurStationStatus)
        .join(CurStationStatus, CurStationStatus.station_id == Station.id)
        .where(
            or_(
                CurStationStatus.occupancy_rate >= _FULL_OCCUPANCY_THRESHOLD,
                CurStationStatus.docks_available <= _FULL_DOCKS_THRESHOLD,
            )
        )
        .order_by(CurStationStatus.occupancy_rate.desc())
        .limit(limit_per_type)
    )
    if active_source:
        empty_stmt = empty_stmt.where(Station.source_system == active_source)
        full_stmt = full_stmt.where(Station.source_system == active_source)
    else:
        empty_stmt = empty_stmt.where(Station.source_system == "mock")
        full_stmt = full_stmt.where(Station.source_system == "mock")

    empty_rows = session.exec(empty_stmt).all()
    full_rows = session.exec(full_stmt).all()

    empty_risks = [_build_risk_item(session, s, c, "empty") for s, c in empty_rows]
    full_risks = [_build_risk_item(session, s, c, "full") for s, c in full_rows]

    return RisksResponse(empty_risks=empty_risks, full_risks=full_risks)


def _build_dynamic_risk_alerts(
    session: Session,
    active_source: str,
    existing_items: list[RecentAlertItem],
    level_filter: str | None = None,
    limit: int = 100,
) -> list[RecentAlertItem]:
    """根据当前 cur_station_status 为真实数据源动态生成风险告警.

    与 existing_items（alert 表已有记录）按 (station_code, type) 去重，
    避免同一站点同一风险重复展示。
    """
    # 已有告警的去重 key
    existing_keys = {
        (a.station_code, a.type) for a in existing_items if a.station_code
    }

    rows = session.exec(
        select(Station, CurStationStatus)
        .join(CurStationStatus, CurStationStatus.station_id == Station.id, isouter=True)
        .where(Station.source_system == active_source)
    ).all()

    dynamic_items: list[RecentAlertItem] = []
    for station, status in rows:
        risk_type = _classify_risk_type(station, status)
        if risk_type == "normal":
            continue

        key = (station.code, risk_type)
        if key in existing_keys:
            continue

        # level 映射
        if risk_type == "offline":
            level = "critical"
        elif risk_type == "abnormal":
            level = "critical"
        elif risk_type == "empty":
            level = "critical" if (status and status.bikes_available == 0) else "warning"
        elif risk_type == "full":
            level = "critical" if (status and status.docks_available == 0) else "warning"
        else:
            level = "info"

        if level_filter and level != level_filter:
            continue

        # 生成文案
        if risk_type == "empty":
            title = f"{station.name} 空车风险"
            bikes = status.bikes_available if status else 0
            message = f"该站点可借车辆仅 {bikes} 辆，需调度补车。"
        elif risk_type == "full":
            title = f"{station.name} 满桩风险"
            docks = status.docks_available if status else 0
            occ = round((status.occupancy_rate * 100) if status else 0, 1)
            message = f"该站点可还桩位仅 {docks} 位（占用率 {occ}%），需调度疏车。"
        elif risk_type == "offline":
            title = f"{station.name} 站点离线"
            message = "站点状态不可用，请检查设备或通信链路。"
        elif risk_type == "abnormal":
            title = f"{station.name} 服务异常"
            renting = status.is_renting if status else False
            returning = status.is_returning if status else False
            parts: list[str] = []
            if not renting:
                parts.append("不可租车")
            if not returning:
                parts.append("不可还车")
            reason = "、".join(parts) if parts else "租还功能异常"
            message = f"站点{reason}，需人工核查。"
        else:
            title = f"{station.name} 异常"
            message = "站点出现未知异常，请检查。"

        region_code: Optional[str] = None
        region_name: Optional[str] = None
        if station.region_id:
            region = session.get(Region, station.region_id)
            region_code = region.code if region else None
            region_name = region.name if region else None

        created_at = status.updated_at if status else _utcnow_naive()

        # 虚拟 ID：负整数，避免与真实 alert id 冲突
        type_code = {"empty": 1, "full": 2, "offline": 3, "abnormal": 4}.get(risk_type, 0)
        virtual_id = -(station.id or 0) * 10 - type_code

        dynamic_items.append(
            RecentAlertItem(
                id=virtual_id,
                level=level,
                type=risk_type,
                title=title,
                message=message,
                status="open",
                station_code=station.code,
                station_name=station.name,
                region_code=region_code,
                region_name=region_name,
                created_at=created_at,
            )
        )

    # 排序：critical > warning > info，同级别按时间倒序
    level_order = {"critical": 0, "warning": 1, "info": 2}
    dynamic_items.sort(key=lambda a: a.created_at, reverse=True)
    dynamic_items.sort(key=lambda a: level_order.get(a.level, 3))
    return dynamic_items[:limit]


def get_recent_alerts(
    session: Session, limit: int = 5, mode: str = "real"
) -> list[RecentAlertItem]:
    """最近未处理告警.

    mode="real": 只返回当前活跃真实数据源的告警；
    mode="mock": 只返回 Mock 演示告警。
    """
    if mode == "real":
        active_source = _get_active_source_filter(session)
        if not active_source:
            return []
        station_filter = Station.source_system == active_source
    else:
        station_filter = Station.source_system == "mock"

    rows = session.exec(
        select(Alert)
        .join(Station, Station.id == Alert.station_id)
        .where(Alert.status == "open", station_filter)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    ).all()

    items: list[RecentAlertItem] = []
    for alert in rows:
        station_code: Optional[str] = None
        station_name: Optional[str] = None
        region_code: Optional[str] = None
        region_name: Optional[str] = None
        if alert.station_id:
            station = session.get(Station, alert.station_id)
            station_code = station.code if station else None
            station_name = station.name if station else None
            if station and station.region_id:
                region = session.get(Region, station.region_id)
                region_code = region.code if region else None
                region_name = region.name if region else None
        elif alert.region_id:
            region = session.get(Region, alert.region_id)
            region_code = region.code if region else None
            region_name = region.name if region else None

        items.append(
            RecentAlertItem(
                id=alert.id or 0,
                level=alert.level,
                type=alert.type,
                title=alert.title,
                message=alert.message,
                status=alert.status,
                station_code=station_code,
                station_name=station_name,
                region_code=region_code,
                region_name=region_name,
                created_at=alert.created_at,
            )
        )

    # real 模式下补充动态风险告警
    if mode == "real" and active_source:
        dynamic = _build_dynamic_risk_alerts(session, active_source, items, limit=limit)
        items.extend(dynamic)
        # 重新排序并截断
        level_order = {"critical": 0, "warning": 1, "info": 2}
        items.sort(key=lambda a: a.created_at, reverse=True)
        items.sort(key=lambda a: level_order.get(a.level, 3))
        items = items[:limit]

    return items


def get_etl_health(session: Session) -> list[EtlFeedHealth]:
    feeds = session.exec(select(GbfsFeed)).all()
    items: list[EtlFeedHealth] = []
    since = _utcnow_naive() - timedelta(hours=24)

    for feed in feeds:
        source = session.get(DataSource, feed.source_id)
        latest_log = session.exec(
            select(FetchLog)
            .where(FetchLog.feed_id == feed.id)
            .order_by(FetchLog.started_at.desc())
            .limit(1)
        ).first()

        recent_failures = session.exec(
            select(func.count(FetchLog.id))
            .where(
                FetchLog.feed_id == feed.id,
                FetchLog.status == "failed",
                FetchLog.started_at >= since,
            )
        ).one() if feed.id else 0

        items.append(
            EtlFeedHealth(
                feed_id=feed.id or 0,
                feed_name=feed.feed_name,
                source_name=source.name if source else "unknown",
                last_status=latest_log.status if latest_log else None,
                last_started_at=latest_log.started_at if latest_log else None,
                last_duration_ms=latest_log.duration_ms if latest_log else None,
                last_error=latest_log.error if latest_log else None,
                recent_failures=int(recent_failures or 0),
            )
        )
    return items


def get_summary(session: Session) -> DashboardSummary:
    risks = get_risks(session, limit_per_type=5)
    return DashboardSummary(
        kpi=get_kpi(session),
        region_ranking=get_region_ranking(session, limit=5),
        operational_area_ranking=get_operational_area_ranking(session, limit=10),
        risk_stations=[*risks.empty_risks, *risks.full_risks],
        recent_alerts=get_recent_alerts(session, limit=5),
        etl_health=get_etl_health(session),
    )


# ---------------------------------------------------------------------------
# M2.2-A：站点 GeoJSON（首页大屏散点图）
# ---------------------------------------------------------------------------


def _classify_risk_type(
    station: Station, status: Optional[CurStationStatus]
) -> str:
    """5 类风险互斥分桶，按优先级判定：

    1. ``offline``  — 站点 status=closed 或当前没有任何状态行
    2. ``abnormal`` — 站点 status=maintenance，或当前 is_renting / is_returning 关闭
    3. ``empty``    — 在线 + 可借车数 ≤ 阈值
    4. ``full``     — 在线 + 占用率 ≥ 阈值
    5. ``normal``   — 其他
    """
    if station.status == "closed" or status is None:
        return "offline"
    if station.status == "maintenance":
        return "abnormal"
    # status == active 之后再判断动态
    if not status.is_renting or not status.is_returning:
        return "abnormal"
    if status.bikes_available <= _EMPTY_BIKES_THRESHOLD:
        return "empty"
    if (
        status.occupancy_rate >= _FULL_OCCUPANCY_THRESHOLD
        or status.docks_available <= _FULL_DOCKS_THRESHOLD
    ):
        return "full"
    return "normal"


def get_stations_geojson(session: Session) -> StationGeoFeatureCollection:
    """所有站点的 GeoJSON FeatureCollection，供 MapLibre 散点层使用."""
    active_source = _get_active_source_filter(session)

    stmt = (
        select(Station, CurStationStatus, Region)
        .join(
            CurStationStatus,
            CurStationStatus.station_id == Station.id,
            isouter=True,
        )
        .join(Region, Region.id == Station.region_id, isouter=True)
    )
    if active_source:
        stmt = stmt.where(Station.source_system == active_source)
    else:
        stmt = stmt.where(Station.source_system == "mock")

    rows = session.exec(stmt).all()

    features: list[StationGeoFeature] = []
    for station, status, region in rows:
        risk_type = _classify_risk_type(station, status)

        # 缺 cur_status 时，bikes/docks/占用率 全部置 0，但 capacity 来自 station 表
        bikes = status.bikes_available if status else 0
        docks = status.docks_available if status else 0
        occ = float(round(status.occupancy_rate, 4)) if status else 0.0
        updated_at = status.updated_at if status else None

        features.append(
            StationGeoFeature(
                geometry=StationGeometry(coordinates=[station.lng, station.lat]),
                properties=StationGeoProperties(
                    station_id=station.id or 0,
                    station_code=station.code,
                    name=station.name,
                    region_name=region.name if region else None,
                    capacity=int(station.capacity or 0),
                    bikes_available=int(bikes),
                    docks_available=int(docks),
                    occupancy_rate=occ,
                    risk_type=risk_type,  # type: ignore[arg-type]
                    status=station.status,
                    updated_at=updated_at,
                ),
            )
        )

    return StationGeoFeatureCollection(features=features)


# ---------------------------------------------------------------------------
# M4：多功能页面数据 API
# ---------------------------------------------------------------------------


def get_regions(session: Session) -> list[RegionRankingItem]:
    """全部区域列表（不限条数），供 /regions 页面使用."""
    return get_region_ranking(session, limit=9999)


def get_stations(
    session: Session,
    risk_type: str | None = None,
) -> list[RiskStationItem]:
    """全部站点列表，支持 risk_type 筛选."""
    active_source = _get_active_source_filter(session)

    stmt = (
        select(Station, CurStationStatus)
        .join(CurStationStatus, CurStationStatus.station_id == Station.id, isouter=True)
    )
    if active_source:
        stmt = stmt.where(Station.source_system == active_source)
    else:
        stmt = stmt.where(Station.source_system == "mock")

    rows = session.exec(stmt).all()

    items: list[RiskStationItem] = []
    for station, status in rows:
        classified = _classify_risk_type(station, status)
        if risk_type and classified != risk_type:
            continue
        if status is None:
            # 构造一个空状态用于 offline 展示
            status = CurStationStatus(
                station_id=station.id,
                bikes_available=0,
                docks_available=0,
                occupancy_rate=0.0,
            )
        items.append(_build_risk_item(session, station, status, classified))

    return items


def get_alerts(
    session: Session,
    level: str | None = None,
    status: str = "open",
    limit: int = 100,
    mode: str = "real",
) -> list[RecentAlertItem]:
    """告警列表，支持级别和状态筛选，供 /alerts 页面使用.

    mode="real": 只返回当前活跃真实数据源的告警；
    mode="mock": 只返回 Mock 演示告警。
    """
    active_source = _get_active_source_filter(session)

    if mode == "real":
        if not active_source:
            return []
        station_filter = Station.source_system == active_source
    else:
        station_filter = Station.source_system == "mock"

    stmt = (
        select(Alert)
        .join(Station, Station.id == Alert.station_id)
        .where(Alert.status == status, station_filter)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    if level:
        stmt = stmt.where(Alert.level == level)

    rows = session.exec(stmt).all()

    items: list[RecentAlertItem] = []
    for alert in rows:
        station_code: Optional[str] = None
        station_name: Optional[str] = None
        region_code: Optional[str] = None
        region_name: Optional[str] = None
        if alert.station_id:
            station = session.get(Station, alert.station_id)
            station_code = station.code if station else None
            station_name = station.name if station else None
            if station and station.region_id:
                region = session.get(Region, station.region_id)
                region_code = region.code if region else None
                region_name = region.name if region else None
        elif alert.region_id:
            region = session.get(Region, alert.region_id)
            region_code = region.code if region else None
            region_name = region.name if region else None

        items.append(
            RecentAlertItem(
                id=alert.id or 0,
                level=alert.level,
                type=alert.type,
                title=alert.title,
                message=alert.message,
                status=alert.status,
                station_code=station_code,
                station_name=station_name,
                region_code=region_code,
                region_name=region_name,
                created_at=alert.created_at,
            )
        )

    # real 模式下补充动态风险告警（仅限 open 状态）
    if mode == "real" and active_source and status == "open":
        dynamic = _build_dynamic_risk_alerts(
            session, active_source, items, level_filter=level, limit=limit
        )
        items.extend(dynamic)
        # 重新排序并截断
        level_order = {"critical": 0, "warning": 1, "info": 2}
        items.sort(key=lambda a: a.created_at, reverse=True)
        items.sort(key=lambda a: level_order.get(a.level, 3))
        items = items[:limit]

    return items


# ---------------------------------------------------------------------------
# M2.2-B：24h 时序趋势（首页 TrendPanel）
# ---------------------------------------------------------------------------


_TREND_HOURS = 24
_TREND_TOP_REGIONS = 3


def _bucket_floor(now: datetime) -> datetime:
    """把 now 向下对齐到整点，返回 naive UTC."""
    return now.replace(minute=0, second=0, microsecond=0)


def get_dashboard_trends_24h(session: Session) -> DashboardTrend24h:
    """近 24 小时趋势（每小时 1 桶），全部聚合自 ``fact_station_snapshot``.

    - city_total_bikes / city_avg_occupancy：跨所有站点按 ts 桶聚合
    - alerts_count：按 ``Alert.created_at`` 落桶（注意区间是 [ts, ts+1h)）
    - region_series：取站点数 Top 3 的区域，每区每桶 SUM(bikes) + AVG(occ)
    """
    now = _utcnow_naive()
    end = _bucket_floor(now) + timedelta(hours=1)  # 包含当前小时
    start = end - timedelta(hours=_TREND_HOURS)

    # 1) 城市层面：按 ts 桶 sum/avg
    city_rows = session.exec(
        select(
            FactStationSnapshot.ts,
            func.coalesce(func.sum(FactStationSnapshot.bikes), 0),
            func.coalesce(func.avg(FactStationSnapshot.occupancy_rate), 0.0),
        )
        .where(FactStationSnapshot.ts >= start, FactStationSnapshot.ts < end)
        .group_by(FactStationSnapshot.ts)
        .order_by(FactStationSnapshot.ts.asc())
    ).all()

    city_map: dict[datetime, tuple[int, float]] = {
        ts: (int(total or 0), float(round(avg_occ or 0.0, 4)))
        for ts, total, avg_occ in city_rows
    }

    # 2) 告警按小时桶（SQLite-friendly：strftime 截到小时）
    alert_rows = session.exec(
        select(
            func.strftime("%Y-%m-%d %H:00:00", Alert.created_at),
            func.count(Alert.id),
        )
        .where(Alert.created_at >= start, Alert.created_at < end)
        .group_by(func.strftime("%Y-%m-%d %H:00:00", Alert.created_at))
    ).all()
    alert_map: dict[str, int] = {str(k): int(v or 0) for k, v in alert_rows}

    # 3) 装配 buckets：以 _TREND_HOURS 个整点 ts 为基准（即使某些 ts 没数据也补 0）
    buckets: list[TrendBucket] = []
    for i in range(_TREND_HOURS):
        ts = start + timedelta(hours=i)
        total, avg_occ = city_map.get(ts, (0, 0.0))
        ts_key = ts.strftime("%Y-%m-%d %H:00:00")
        buckets.append(
            TrendBucket(
                ts=ts,
                city_total_bikes=total,
                city_avg_occupancy=avg_occ,
                alerts_count=alert_map.get(ts_key, 0),
            )
        )

    # 4) Top 3 区域：按站点数排序（稳定且与首页 region_ranking 风格一致）
    top_region_rows = session.exec(
        select(Region, func.count(Station.id))
        .join(Station, Station.region_id == Region.id, isouter=True)
        .group_by(Region.id)
        .order_by(func.count(Station.id).desc(), Region.id.asc())
        .limit(_TREND_TOP_REGIONS)
    ).all()
    top_regions = [r for r, _ in top_region_rows]
    top_region_ids = [r.id for r in top_regions if r.id is not None]

    region_series: list[TrendRegionSeries] = []
    if top_region_ids:
        region_rows = session.exec(
            select(
                Region.id,
                Region.code,
                Region.name,
                FactStationSnapshot.ts,
                func.coalesce(func.sum(FactStationSnapshot.bikes), 0),
                func.coalesce(func.avg(FactStationSnapshot.occupancy_rate), 0.0),
            )
            .join(Station, Station.id == FactStationSnapshot.station_id)
            .join(Region, Region.id == Station.region_id)
            .where(
                FactStationSnapshot.ts >= start,
                FactStationSnapshot.ts < end,
                Region.id.in_(top_region_ids),
            )
            .group_by(Region.id, Region.code, Region.name, FactStationSnapshot.ts)
            .order_by(Region.id.asc(), FactStationSnapshot.ts.asc())
        ).all()

        # 收集每区域 ts → (bikes, occ)
        per_region: dict[int, dict[datetime, tuple[int, float]]] = {}
        region_meta: dict[int, tuple[str, str]] = {}
        for rid, code, name, ts, total, avg_occ in region_rows:
            region_meta[int(rid)] = (str(code), str(name))
            per_region.setdefault(int(rid), {})[ts] = (
                int(total or 0),
                float(round(avg_occ or 0.0, 4)),
            )

        # 保持 Top3 原顺序输出
        for region in top_regions:
            rid = region.id
            if rid is None:
                continue
            data = per_region.get(rid, {})
            code, name = region_meta.get(rid, (region.code, region.name))
            points = [
                TrendRegionPoint(
                    ts=start + timedelta(hours=i),
                    bikes_available=data.get(start + timedelta(hours=i), (0, 0.0))[0],
                    avg_occupancy=data.get(start + timedelta(hours=i), (0, 0.0))[1],
                )
                for i in range(_TREND_HOURS)
            ]
            region_series.append(
                TrendRegionSeries(
                    region_code=code,
                    region_name=name,
                    points=points,
                )
            )

    return DashboardTrend24h(buckets=buckets, region_series=region_series)


# ---------------------------------------------------------------------------
# M5.1: 单站详情
# ---------------------------------------------------------------------------


def get_station_detail(session: Session, code: str) -> StationDetail | None:
    """按 station.code 查单站详情（静态信息 + 当前状态 + 区域）."""
    station = session.exec(
        select(Station).where(Station.code == code)
    ).first()
    if station is None:
        return None

    status = session.exec(
        select(CurStationStatus).where(CurStationStatus.station_id == station.id)
    ).first()

    region: Optional[Region] = None
    if station.region_id:
        region = session.get(Region, station.region_id)

    risk_type = _classify_risk_type(station, status)

    return StationDetail(
        station_id=station.id or 0,
        station_code=station.code,
        name=station.name,
        region_code=region.code if region else None,
        region_name=region.name if region else None,
        lat=station.lat,
        lng=station.lng,
        capacity=station.capacity,
        status=station.status,
        bikes_available=status.bikes_available if status else 0,
        docks_available=status.docks_available if status else 0,
        occupancy_rate=float(round(status.occupancy_rate, 4)) if status else 0.0,
        risk_type=risk_type,  # type: ignore[arg-type]
        updated_at=status.updated_at if status else None,
    )


def get_station_history(
    session: Session, code: str, hours: int = 24
) -> StationHistoryResponse | None:
    """按 station.code 查最近 N 小时历史快照（fact_station_snapshot）."""
    station = session.exec(
        select(Station).where(Station.code == code)
    ).first()
    if station is None:
        return None

    now = _utcnow_naive()
    end = _bucket_floor(now) + timedelta(hours=1)
    start = end - timedelta(hours=hours)

    rows = session.exec(
        select(
            FactStationSnapshot.ts,
            FactStationSnapshot.bikes,
            FactStationSnapshot.docks,
            FactStationSnapshot.occupancy_rate,
        )
        .where(
            FactStationSnapshot.station_id == station.id,
            FactStationSnapshot.ts >= start,
            FactStationSnapshot.ts < end,
        )
        .order_by(FactStationSnapshot.ts.asc())
    ).all()

    points = [
        StationHistoryPoint(
            ts=ts,
            bikes_available=int(bikes or 0),
            docks_available=int(docks or 0),
            occupancy_rate=float(round(occ or 0.0, 4)),
        )
        for ts, bikes, docks, occ in rows
    ]

    return StationHistoryResponse(
        station_code=code,
        range="24h",
        interval="1h",
        points=points,
    )


def get_station_alerts(
    session: Session, code: str, limit: int = 20
) -> StationAlertsResponse | None:
    """按 station.code 查该站点相关告警."""
    station = session.exec(
        select(Station).where(Station.code == code)
    ).first()
    if station is None:
        return None

    rows = session.exec(
        select(Alert)
        .where(Alert.station_id == station.id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    ).all()

    items = [
        StationAlertItem(
            id=alert.id or 0,
            level=alert.level,
            type=alert.type,
            title=alert.title,
            message=alert.message,
            status=alert.status,
            created_at=alert.created_at,
        )
        for alert in rows
    ]

    return StationAlertsResponse(station_code=code, items=items)
