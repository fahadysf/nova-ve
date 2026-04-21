from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.dependencies import get_current_user
from app.schemas.user import UserRead
from app.services.node_runtime_service import NodeRuntimeService
import psutil

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def healthcheck(
    db: AsyncSession = Depends(get_db),
):
    await db.execute(text("SELECT 1"))
    return {
        "code": 200,
        "status": "success",
        "message": "Healthcheck passed.",
        "data": {"database": "ok"},
    }


@router.get("/status")
async def get_status(
    current_user: UserRead = Depends(get_current_user),
):
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    runtime_counts = NodeRuntimeService().runtime_counts()
    return {
        "code": 200,
        "status": "success",
        "message": "Fetched system status (60001).",
        "data": {
            "version": "0.1.0-alpha",
            "qemu_version": "8.0.0",
            "uksm": "unsupported",
            "ksm": "unsupported",
            "cpulimit": "enabled",
            "cpu": str(psutil.cpu_count(logical=False)),
            "vCPU": str(psutil.cpu_count(logical=True)),
            "disk": f"{disk.used / (1024**3):.4f}",
            "diskavailable": f"{disk.free / (1024**3):.4f}",
            "mindisk": 10,
            "memtotal": str(mem.total // 1024),
            "cached": 0,
            "mem": f"{mem.percent:.4f}",
            "swap": "0.0000",
            "swapavailable": "8388604",
            "iol": runtime_counts["iol"],
            "dynamips": runtime_counts["dynamips"],
            "qemu": runtime_counts["qemu"],
            "docker": runtime_counts["docker"],
            "vpcs": runtime_counts["vpcs"],
        },
    }


@router.get("/system/settings")
async def get_settings_route(
    current_user: UserRead = Depends(get_current_user),
):
    return {
        "code": 201,
        "status": "success",
        "message": "System settings successfully fetched (60078).",
        "data": {
            "radius_server_ip": "0.0.0.0",
            "radius_server_port": 1812,
            "radius_server_secret": "secret",
            "ad_server_ip": "0.0.0.0",
            "ad_server_port": 389,
            "ad_server_tls": 0,
            "template_disabled": ".missing",
            "lic_check": "none",
            "mindisk": 10,
            "vpn_net": "172.29.130",
            "docker_net": "172.17",
            "nat_net": "172.29.129",
            "color_scheme": "white-black",
            "font_size": 11,
            "font_name": "DejaVu Sans Mono",
            "ipv6": 1,
            "caching": 1,
            "cpudedicate": "0",
            "numa": "0",
            "realtime_update": "eco",
        },
    }


@router.get("/poll")
async def poll(
    current_user: UserRead = Depends(get_current_user),
):
    disk = psutil.disk_usage("/")
    return {
        "code": 200,
        "status": "success",
        "data": {
            "diskavailable": f"{disk.free / (1024**3):.4f}",
            "disk": f"{disk.used / (1024**3):.4f}",
            "mindisk": 10,
        },
    }
