"""
SSD 卸载到 HDD —— MoviePilot V2 插件
监听 TransferComplete 事件，整理完成后调用 qBittorrent setLocation
把 SSD 缓存盘上的种子数据搬到机械盘上，让 tr 接管做种时直接在 HDD 上。

依赖：
    - MoviePilot V2 (>= v2.4.x，事件系统已稳定)
    - qbittorrent-api (MP 内置)

工作流：
    qb 下载到 SSD -> MP 整理 rclone_copy 到 115（hash 在 SSD 上算）
                  -> TransferComplete 事件触发本插件
                  -> 本插件调用 qb.torrents_set_location() 把数据移到 HDD
                  -> qb 自己把文件从 SSD 搬到 HDD，继续做种，不掉种
                  -> 自动转移做种插件下次轮询时，从 BT_backup 读 .torrent
                     加到 tr，save_path 已经是 HDD（因为 qb 已经搬过了）
"""
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager, Event
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType


class SsdOffload(_PluginBase):
    # ---- 插件元信息 ----
    plugin_name = "SSD 卸载到 HDD"
    plugin_desc = "整理完成后把 qb 中位于 SSD 缓存盘的种子数据搬到机械盘，搬运由 qb setLocation 完成，不掉种。"
    plugin_icon = "https://raw.githubusercontent.com/Cyrker/MoviePilot-Plugins/main/icons/ssdoffload.png"
    plugin_version = "1.1.0"
    plugin_author = "Cyrker"
    author_url = "https://github.com/Cyrker/MoviePilot-Plugins"
    plugin_config_prefix = "ssdoffload_"
    plugin_order = 30
    auth_level = 1

    # ---- 内部状态 ----
    _enabled: bool = False
    _notify: bool = False
    _ssd_prefix: str = ""
    _hdd_prefixes: List[str] = []
    _strategy: str = "most_free"
    _downloader_name: str = ""
    _required_tag: str = ""
    _delay_seconds: int = 5
    _dry_run: bool = False
    # 用于 round_robin 策略的指针，进程内简单计数
    _rr_index: int = 0
    _lock = threading.Lock()

    # 下载器辅助
    downloader_helper: Optional[DownloaderHelper] = None

    # ---------------------------------------------------------------------
    # 初始化
    # ---------------------------------------------------------------------
    def init_plugin(self, config: dict = None):
        self.downloader_helper = DownloaderHelper()
        if not config:
            return

        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._ssd_prefix = (config.get("ssd_prefix") or "").rstrip("/")
        hdd_raw = config.get("hdd_prefixes") or ""
        if isinstance(hdd_raw, list):
            self._hdd_prefixes = [p.rstrip("/") for p in hdd_raw if p]
        else:
            self._hdd_prefixes = [
                p.strip().rstrip("/") for p in str(hdd_raw).splitlines() if p.strip()
            ]
        self._strategy = config.get("strategy") or "most_free"
        self._downloader_name = (config.get("downloader_name") or "").strip()
        self._required_tag = (config.get("required_tag") or "").strip()
        try:
            self._delay_seconds = int(config.get("delay_seconds") or 5)
        except (TypeError, ValueError):
            self._delay_seconds = 5
        self._dry_run = bool(config.get("dry_run", False))

        logger.info(
            f"【SsdOffload】初始化完成: enabled={self._enabled}, "
            f"ssd={self._ssd_prefix}, hdd={self._hdd_prefixes}, "
            f"strategy={self._strategy}, downloader={self._downloader_name or '默认'}, "
            f"tag={self._required_tag or '无'}, delay={self._delay_seconds}s, "
            f"dry_run={self._dry_run}"
        )

    def get_state(self) -> bool:
        return bool(
            self._enabled
            and self._ssd_prefix
            and self._hdd_prefixes
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    # ---------------------------------------------------------------------
    # 事件处理
    # ---------------------------------------------------------------------
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        if not self.get_state():
            return

        try:
            self._handle_event(event)
        except Exception as e:
            logger.error(
                f"【SsdOffload】处理 TransferComplete 事件异常: {e}\n{traceback.format_exc()}"
            )

    def _handle_event(self, event: Event):
        event_data: dict = event.event_data or {}

        # MoviePilot V2 的 TransferComplete event_data 字段（按版本可能略有差异）：
        #   - fileitem: 源文件 FileItem
        #   - meta: MetaBase
        #   - mediainfo: MediaInfo
        #   - transferinfo: TransferInfo
        #   - downloader: 下载器名称
        #   - download_hash: 种子 hash
        # 这里用 .get 安全访问，缺字段则跳过
        download_hash: Optional[str] = (
            event_data.get("download_hash")
            or event_data.get("downloader_hash")
            or event_data.get("torrent_hash")
        )
        downloader_name: Optional[str] = event_data.get("downloader")
        fileitem = event_data.get("fileitem")
        src_path: Optional[str] = None
        if fileitem is not None:
            # FileItem 是 pydantic 模型，path 是字符串
            src_path = getattr(fileitem, "path", None) or (
                fileitem.get("path") if isinstance(fileitem, dict) else None
            )

        if not download_hash:
            logger.debug("【SsdOffload】事件中无 download_hash，跳过")
            return
        if not src_path:
            logger.debug("【SsdOffload】事件中无源文件路径，跳过")
            return

        # 只处理位于 SSD 前缀下的文件
        if not self._is_under(src_path, self._ssd_prefix):
            logger.debug(
                f"【SsdOffload】源路径 {src_path} 不在 SSD 前缀 {self._ssd_prefix} 下，跳过"
            )
            return

        # 指定了下载器名称就只处理对应下载器
        if self._downloader_name and downloader_name and downloader_name != self._downloader_name:
            logger.debug(
                f"【SsdOffload】下载器 {downloader_name} 不匹配过滤项 {self._downloader_name}，跳过"
            )
            return

        # 找到 qb 客户端
        qb_service = self._get_qb_service(downloader_name)
        if qb_service is None:
            logger.warning(
                f"【SsdOffload】未找到可用的 qBittorrent 下载器 (filter={self._downloader_name})"
            )
            return

        # 异步等一下再搬，给 rclone 收尾、给 TransferHistory 落库留时间
        delay = max(0, self._delay_seconds)
        if delay:
            t = threading.Thread(
                target=self._delayed_move,
                args=(qb_service, download_hash, src_path, delay),
                daemon=True,
            )
            t.start()
        else:
            self._do_move(qb_service, download_hash, src_path)

    def _delayed_move(self, qb_service, download_hash: str, src_path: str, delay: int):
        time.sleep(delay)
        try:
            self._do_move(qb_service, download_hash, src_path)
        except Exception as e:
            logger.error(
                f"【SsdOffload】延时搬运异常 hash={download_hash}: {e}\n{traceback.format_exc()}"
            )

    # ---------------------------------------------------------------------
    # 核心搬运逻辑
    # ---------------------------------------------------------------------
    def _do_move(self, qb_service, download_hash: str, src_path: str):
        qb_client = self._extract_qbittorrent_api(qb_service)
        if qb_client is None:
            logger.error("【SsdOffload】无法获取 qbittorrent-api 实例")
            return

        # 取这条种当前的 save_path
        try:
            torrents = qb_client.torrents_info(torrent_hashes=download_hash)
        except Exception as e:
            logger.error(f"【SsdOffload】查询种子 {download_hash} 失败: {e}")
            return
        if not torrents:
            logger.warning(f"【SsdOffload】qb 中找不到种子 hash={download_hash}")
            return
        torrent = torrents[0]
        current_save_path: str = (torrent.save_path or "").rstrip("/")
        torrent_name: str = torrent.name or ""
        torrent_size: int = int(torrent.size or 0)
        torrent_tags: str = torrent.tags or ""

        # 标签过滤
        if self._required_tag:
            tags = [t.strip() for t in torrent_tags.split(",") if t.strip()]
            if self._required_tag not in tags:
                logger.debug(
                    f"【SsdOffload】种子 {torrent_name} 缺少标签 {self._required_tag}，跳过"
                )
                return

        # 校验当前确实在 SSD 上
        if not self._is_under(current_save_path, self._ssd_prefix):
            logger.info(
                f"【SsdOffload】种子 {torrent_name} 当前 save_path={current_save_path} 已不在 SSD，跳过"
            )
            return

        # 选目标 HDD 前缀，并算出新的 save_path
        target_hdd = self._pick_target_hdd(torrent_size)
        if not target_hdd:
            logger.error("【SsdOffload】没有可用的 HDD 前缀（全部不可达或空间不足）")
            return

        # 用前缀替换的方式保留 SSD 上的子目录结构
        # 例: /media/disk4-150G/downloads/电影  ->  /media/disk2-16T/downloads/电影
        relative = current_save_path[len(self._ssd_prefix):].lstrip("/")
        new_save_path = (
            target_hdd if not relative else f"{target_hdd}/{relative}"
        )

        # 容错：如果种子本身已经下载完成才能搬（qb 在搬运过程中会暂停 IO，未完成的会被打断）
        progress = float(torrent.progress or 0)
        if progress < 1.0:
            logger.info(
                f"【SsdOffload】种子 {torrent_name} 进度 {progress*100:.1f}% 未完成，跳过"
            )
            return

        # 干跑模式
        if self._dry_run:
            logger.info(
                f"【SsdOffload】[DRY RUN] 将搬运 {torrent_name} "
                f"({torrent_size/1024/1024/1024:.2f} GB): {current_save_path} -> {new_save_path}"
            )
            return

        # 真·调用 qb setLocation
        # qb 在跨盘场景下会以 copy+delete 方式完成搬运，期间种子会变成 "moving" 状态，
        # 完成后自动恢复做种，原始 SSD 上的文件被删除
        try:
            logger.info(
                f"【SsdOffload】开始搬运 {torrent_name} "
                f"({torrent_size/1024/1024/1024:.2f} GB): {current_save_path} -> {new_save_path}"
            )
            # 确保目标父目录存在（qb 自己也会建，但提前建一下避免边界情况）
            try:
                Path(new_save_path).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"【SsdOffload】创建目录 {new_save_path} 失败（可继续）：{e}")
            qb_client.torrents_set_location(
                location=new_save_path, torrent_hashes=download_hash
            )
            logger.info(f"【SsdOffload】setLocation 已下发: {torrent_name}")
        except Exception as e:
            logger.error(
                f"【SsdOffload】setLocation 失败 hash={download_hash}: {e}\n{traceback.format_exc()}"
            )
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SSD 卸载到 HDD】搬运失败",
                    text=f"种子: {torrent_name}\n错误: {e}",
                )
            return

        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【SSD 卸载到 HDD】已下发搬运",
                text=(
                    f"种子: {torrent_name}\n"
                    f"大小: {torrent_size/1024/1024/1024:.2f} GB\n"
                    f"{current_save_path}\n→\n{new_save_path}\n"
                    f"qb 后台搬运中，搬完会自动继续做种。"
                ),
            )

    # ---------------------------------------------------------------------
    # 工具：选盘 / 取下载器实例
    # ---------------------------------------------------------------------
    def _pick_target_hdd(self, required_size: int) -> Optional[str]:
        """按策略挑一个目标 HDD 前缀返回。"""
        candidates: List[Tuple[str, int]] = []  # (path, free_bytes)
        for hdd in self._hdd_prefixes:
            try:
                usage = shutil.disk_usage(hdd)
                free = usage.free
            except Exception as e:
                logger.warning(f"【SsdOffload】无法读取 {hdd} 磁盘信息: {e}")
                continue
            # 留 10 GB 余量，避免把盘塞满
            margin = 10 * 1024 * 1024 * 1024
            if free < required_size + margin:
                logger.debug(
                    f"【SsdOffload】{hdd} 剩余 {free/1024**3:.1f}GB 不足以容纳 "
                    f"{required_size/1024**3:.1f}GB（含 10GB 余量），跳过"
                )
                continue
            candidates.append((hdd, free))

        if not candidates:
            return None

        if self._strategy == "round_robin":
            with self._lock:
                idx = self._rr_index % len(candidates)
                self._rr_index = (self._rr_index + 1) % max(1, len(candidates))
            return candidates[idx][0]

        # 默认: most_free
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def _get_qb_service(self, downloader_name: Optional[str]):
        """取出 qBittorrent 类型的下载器服务实例。"""
        helper = self.downloader_helper or DownloaderHelper()

        # 优先按用户配置 / 事件给的下载器名称取
        candidate_names: List[str] = []
        if self._downloader_name:
            candidate_names.append(self._downloader_name)
        if downloader_name and downloader_name not in candidate_names:
            candidate_names.append(downloader_name)

        for name in candidate_names:
            try:
                svc = helper.get_service(name=name)
                if svc and self._is_qbittorrent_service(svc):
                    return svc
            except Exception as e:
                logger.debug(f"【SsdOffload】get_service({name}) 失败: {e}")

        # 兜底：扫一遍所有下载器，挑第一个 qb
        try:
            services = helper.get_services() or {}
            for svc in services.values():
                if self._is_qbittorrent_service(svc):
                    return svc
        except Exception as e:
            logger.debug(f"【SsdOffload】get_services 失败: {e}")
        return None

    @staticmethod
    def _is_qbittorrent_service(svc) -> bool:
        # 通过类名或者 type 属性识别 qb
        try:
            cls_name = type(getattr(svc, "instance", svc)).__name__.lower()
            if "qbittorrent" in cls_name:
                return True
        except Exception:
            pass
        try:
            t = getattr(svc, "type", "") or ""
            if str(t).lower() == "qbittorrent":
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _extract_qbittorrent_api(svc):
        """从 MP 的 qb 服务对象里拿到 qbittorrentapi.Client。

        MP 中常见的层级:
            svc.instance  -> Qbittorrent 包装类
            svc.instance.qbc -> qbittorrentapi.Client
        某些版本可能直接是 svc.instance 即 client，做兼容。
        """
        instance = getattr(svc, "instance", None) or svc
        client = getattr(instance, "qbc", None)
        if client is not None:
            return client
        # 兜底：instance 本身可能就是 qbittorrentapi.Client
        if hasattr(instance, "torrents_set_location"):
            return instance
        return None

    @staticmethod
    def _is_under(path: str, prefix: str) -> bool:
        if not path or not prefix:
            return False
        # 标准化尾斜杠对齐
        p = path.rstrip("/")
        pre = prefix.rstrip("/")
        return p == pre or p.startswith(pre + "/")

    def _get_qb_downloader_options(self) -> List[Dict[str, Any]]:
        """读取 MP 中已配置的 qBittorrent 下载器，构建下拉选项。

        在 get_form() 渲染时调用，每次打开插件配置页都会重新拉取一次，
        新增/删除下载器后无需重启 MP。
        """
        options: List[Dict[str, Any]] = [
            {"title": "自动（使用第一个 qBittorrent）", "value": ""}
        ]
        try:
            helper = self.downloader_helper or DownloaderHelper()
            services = helper.get_services() or {}
            for name, svc in services.items():
                if not self._is_qbittorrent_service(svc):
                    continue
                # 尝试展示 enabled 状态，便于用户区分已禁用的下载器
                label = name
                try:
                    cfg = getattr(svc, "config", None)
                    if cfg is not None and hasattr(cfg, "enabled") and not cfg.enabled:
                        label = f"{name}（已禁用）"
                except Exception:
                    pass
                options.append({"title": label, "value": name})
        except Exception as e:
            logger.warning(f"【SsdOffload】枚举下载器列表失败: {e}")
        return options

    def stop_service(self):
        pass

    # ---------------------------------------------------------------------
    # UI 配置表单
    # ---------------------------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "notify",
                                            "label": "搬运通知",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "dry_run",
                                            "label": "仅日志（不实际搬）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ssd_prefix",
                                            "label": "SSD 前缀（容器内）",
                                            "placeholder": "/media/disk4-150G",
                                            "hint": "下载在该路径下的种子才会被搬运",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "strategy",
                                            "label": "选盘策略",
                                            "items": [
                                                {"title": "剩余空间最多", "value": "most_free"},
                                                {"title": "轮询", "value": "round_robin"},
                                            ],
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "hdd_prefixes",
                                            "label": "HDD 前缀（每行一个，容器内路径）",
                                            "placeholder": "/media/disk1-8T\n/media/disk2-16T\n/media/disk3-16T",
                                            "rows": 3,
                                            "hint": "插件会按所选策略从这些盘里挑一个作为目标",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader_name",
                                            "label": "下载器",
                                            "items": self._get_qb_downloader_options(),
                                            "hint": "选择 MP 中已配置的 qBittorrent 下载器",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "required_tag",
                                            "label": "种子标签过滤（可选）",
                                            "placeholder": "已整理",
                                            "hint": "只搬运带此标签的种子",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_seconds",
                                            "label": "搬运延时（秒）",
                                            "type": "number",
                                            "hint": "等 rclone 上传收尾，默认 5 秒",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": (
                                "工作原理：监听 MoviePilot 的 TransferComplete 事件，"
                                "当源文件位于 SSD 前缀下时，调用 qb 的 setLocation API 把数据搬到 HDD。"
                                "搬运由 qb 后台完成，做种不掉。容器路径映射要保证 MP / qb / tr 三者完全一致。"
                            ),
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "dry_run": False,
            "ssd_prefix": "/media/disk4-150G",
            "hdd_prefixes": "/media/disk1-8T\n/media/disk2-16T\n/media/disk3-16T",
            "strategy": "most_free",
            "downloader_name": "",
            "required_tag": "",
            "delay_seconds": 5,
        }

    def get_page(self) -> List[dict]:
        return []
