from pydantic import BaseModel, Field
from typing import Optional


class SystemStatus(BaseModel):
    version: str
    qemu_version: str
    uksm: str
    ksm: str
    cpulimit: str
    cpu: str
    vCPU: str
    disk: str
    diskavailable: str
    mindisk: int
    memtotal: str
    cached: int
    mem: str
    swap: str
    swapavailable: str
    iol: int
    dynamips: int
    qemu: int
    docker: int
    vpcs: int


class SystemSettings(BaseModel):
    radius_server_ip: str
    radius_server_port: int
    radius_server_secret: str
    radius_server_ip_2: str
    radius_server_port_2: int
    radius_server_secret_2: str
    ad_server_ip: str
    ad_server_port: int
    ad_server_tls: int
    ad_server_dn: str
    ad_server_group: str
    proxy_server: str
    proxy_port: int
    proxy_user: str
    proxy_password: str
    template_disabled: str
    pnet0_block: int
    lic_check: str
    mindisk: int
    vpn_net: str
    docker_net: str
    nat_net: str
    color_scheme: str
    font_size: int
    font_name: str
    font_list: str
    ipv6: int
    caching: int
    disk_caching: str
    cpudedicate: str
    mproc: int
    numa: str
    realtime_update: str
