"""
SSD 卸载到 HDD —— MoviePilot V2 插件
监听 TransferComplete 事件（MP 上传到 115 完成时触发），
调用源 qBittorrent 的 setLocation 把 SSD 缓存盘上的种子数据搬到预配置的 HDD，
不掉种。后续做种交接由「自动转移做种」等插件完成，本插件不操作目标下载器。

依赖：
    - MoviePilot V2 (>= v2.4.x，事件系统已稳定)
    - qbittorrent-api (MP 内置)

工作流：
    qb 下载到 SSD -> MP 整理 rclone_copy 到 115（hash 在 SSD 上算）
                  -> TransferComplete 事件触发本插件
                  -> 本插件调用 qb.torrents_set_location() 把数据搬到 HDD
                  -> qb 后台 copy+delete 完成搬运，继续做种
                  -> 「自动转移做种」从 BT_backup 读 .torrent 加到 tr，
                     save_path 已经是 HDD（因为 qb 已经搬过了）
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
    plugin_desc = "MP 上传 115 完成后，调用源 qBittorrent 的 setLocation 把 SSD 缓存盘上的种子数据搬到预配置的 HDD，不掉种。后续做种交接由「自动转移做种」插件完成。"
    plugin_icon = "https://raw.githubusercontent.com/Cyrker/MoviePilot-Plugins/main/icons/ssdoffload.png"
    plugin_version = "1.4.0"
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
    _target_downloader_name: str = ""
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
        self._target_downloader_name = (config.get("target_downloader_name") or "").strip()
        self._required_tag = (config.get("required_tag") or "").strip()
        try:
            self._delay_seconds = int(config.get("delay_seconds") or 5)
        except (TypeError, ValueError):
            self._delay_seconds = 5
        self._dry_run = bool(config.get("dry_run", False))

        logger.info(
            f"【SsdOffload】初始化完成: enabled={self._enabled}, "
            f"ssd={self._ssd_prefix}, hdd={self._hdd_prefixes}, "
            f"strategy={self._strategy}, "
            f"downloader={self._downloader_name or '默认'}, "
            f"target_downloader={self._target_downloader_name or '未指定'}, "
            f"tag={self._required_tag or '无'}, delay={self._delay_seconds}s, "
            f"dry_run={self._dry_run}"
        )

        # 「立刻运行一次」：写回 False 并起一个后台线程跑批扫
        if bool(config.get("run_once")):
            config["run_once"] = False
            try:
                self.update_config(config)
            except Exception as e:
                logger.warning(f"【SsdOffload】写回 run_once=False 失败（不影响执行）: {e}")
            threading.Thread(
                target=self._run_once_batch, daemon=True, name="SsdOffloadRunOnce"
            ).start()

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

        # 找到源下载器服务
        src_service = self._get_source_service(downloader_name)
        if src_service is None:
            logger.warning(
                f"【SsdOffload】未找到可用的源下载器 (filter={self._downloader_name})"
            )
            return

        # 异步等一下再搬，给 rclone 收尾、给 TransferHistory 落库留时间
        delay = max(0, self._delay_seconds)
        if delay:
            t = threading.Thread(
                target=self._delayed_move,
                args=(src_service, download_hash, delay),
                daemon=True,
            )
            t.start()
        else:
            self._do_move(src_service, download_hash)

    def _delayed_move(self, src_service, download_hash: str, delay: int):
        time.sleep(delay)
        try:
            self._do_move(src_service, download_hash)
        except Exception as e:
            logger.error(
                f"【SsdOffload】延时搬运异常 hash={download_hash}: {e}\n{traceback.format_exc()}"
            )

    # ---------------------------------------------------------------------
    # 核心搬运逻辑
    # ---------------------------------------------------------------------
    def _do_move(self, qb_service, download_hash: str, *, silent: bool = False) -> str:
        """搬运一个种子。返回 'moved' / 'skipped' / 'failed'。

        silent=True 时抑制单条搬运通知（用于批扫，由调用方汇总）。
        """
        qb_client = self._extract_qbittorrent_api(qb_service)
        if qb_client is None:
            logger.error("【SsdOffload】无法获取 qbittorrent-api 实例")
            return "failed"

        try:
            torrents = qb_client.torrents_info(torrent_hashes=download_hash)
        except Exception as e:
            logger.error(f"【SsdOffload】查询种子 {download_hash} 失败: {e}")
            return "failed"
        if not torrents:
            logger.warning(f"【SsdOffload】qb 中找不到种子 hash={download_hash}")
            return "skipped"

        torrent = torrents[0]
        current_save_path = (torrent.save_path or "").rstrip("/")
        torrent_name = torrent.name or ""
        torrent_size = int(torrent.size or 0)
        torrent_tags = [s.strip() for s in (torrent.tags or "").split(",") if s.strip()]
        progress = float(torrent.progress or 0)

        # 标签过滤
        if self._required_tag and self._required_tag not in torrent_tags:
            logger.debug(
                f"【SsdOffload】种子 {torrent_name} 缺少标签 {self._required_tag}，跳过"
            )
            return "skipped"

        # 校验当前确实在 SSD 上
        if not self._is_under(current_save_path, self._ssd_prefix):
            logger.info(
                f"【SsdOffload】种子 {torrent_name} 当前 save_path={current_save_path} 已不在 SSD，跳过"
            )
            return "skipped"

        # 选目标 HDD 前缀，并算出新的 save_path
        target_hdd = self._pick_target_hdd(torrent_size)
        if not target_hdd:
            logger.error("【SsdOffload】没有可用的 HDD 前缀（全部不可达或空间不足）")
            return "failed"

        # 用前缀替换的方式保留 SSD 上的子目录结构
        # 例: /media/disk4-150G/downloads/电影  ->  /media/disk2-16T/downloads/电影
        relative = current_save_path[len(self._ssd_prefix):].lstrip("/")
        new_save_path = (
            target_hdd if not relative else f"{target_hdd}/{relative}"
        )

        # 容错：种子完成后才能搬（搬运过程中 IO 会暂停，未完成的会被打断）
        if progress < 1.0:
            logger.info(
                f"【SsdOffload】种子 {torrent_name} 进度 {progress*100:.1f}% 未完成，跳过"
            )
            return "skipped"

        if self._dry_run:
            logger.info(
                f"【SsdOffload】[DRY RUN] 将搬运 {torrent_name} "
                f"({torrent_size/1024/1024/1024:.2f} GB): {current_save_path} -> {new_save_path}"
            )
            return "skipped"

        # 提前建目录避免边界情况（qb 自己也会建）
        try:
            Path(new_save_path).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"【SsdOffload】创建目录 {new_save_path} 失败（可继续）：{e}")

        try:
            logger.info(
                f"【SsdOffload】开始搬运 {torrent_name} "
                f"({torrent_size/1024/1024/1024:.2f} GB): {current_save_path} -> {new_save_path}"
            )
            qb_client.torrents_set_location(
                location=new_save_path, torrent_hashes=download_hash
            )
            logger.info(f"【SsdOffload】setLocation 已下发: {torrent_name}")
        except Exception as e:
            logger.error(
                f"【SsdOffload】setLocation 失败 hash={download_hash}: {e}\n{traceback.format_exc()}"
            )
            if self._notify and not silent:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SSD 卸载到 HDD】搬运失败",
                    text=f"种子: {torrent_name}\n错误: {e}",
                )
            return "failed"

        if self._notify and not silent:
            handoff = (
                f"\n后续由 [{self._target_downloader_name}] 接管做种"
                if self._target_downloader_name
                else ""
            )
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【SSD 卸载到 HDD】已下发搬运",
                text=(
                    f"种子: {torrent_name}\n"
                    f"大小: {torrent_size/1024/1024/1024:.2f} GB\n"
                    f"{current_save_path}\n→\n{new_save_path}{handoff}\n"
                    f"qb 后台搬运中，搬完会自动继续做种。"
                ),
            )
        return "moved"

    # ---------------------------------------------------------------------
    # 立刻运行一次：扫全量符合条件的种子并批量搬
    # ---------------------------------------------------------------------
    def _run_once_batch(self):
        if not self.get_state():
            logger.warning("【SsdOffload】立刻运行一次：插件未启用或必填项未配置，跳过")
            return

        src_service = self._get_source_service(None)
        if src_service is None:
            msg = "未找到可用的 qBittorrent，已中止"
            logger.warning(f"【SsdOffload】立刻运行一次：{msg}")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SSD 卸载到 HDD】立刻运行一次",
                    text=msg,
                )
            return

        qb_client = self._extract_qbittorrent_api(src_service)
        if qb_client is None:
            logger.error("【SsdOffload】立刻运行一次：无法获取 qbittorrent-api 实例")
            return

        try:
            all_torrents = qb_client.torrents_info()
        except Exception as e:
            logger.error(f"【SsdOffload】立刻运行一次：列出种子失败: {e}")
            return

        # 先按 SSD 前缀 / 完成度 / 标签过滤一遍，避免无效 _do_move 调用
        matched_hashes: List[str] = []
        for t in all_torrents:
            save_path = (t.save_path or "").rstrip("/")
            if not self._is_under(save_path, self._ssd_prefix):
                continue
            if float(t.progress or 0) < 1.0:
                continue
            if self._required_tag:
                tags = [s.strip() for s in (t.tags or "").split(",") if s.strip()]
                if self._required_tag not in tags:
                    continue
            matched_hashes.append(t.hash)

        total = len(matched_hashes)
        logger.info(f"【SsdOffload】立刻运行一次：扫描到 {total} 个符合条件的种子")

        if total == 0:
            if self._notify:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【SSD 卸载到 HDD】立刻运行一次",
                    text="未找到符合条件的种子（SSD 前缀下、已完成、标签匹配）",
                )
            return

        moved = skipped = failed = 0
        for h in matched_hashes:
            try:
                res = self._do_move(src_service, h, silent=True)
            except Exception as e:
                logger.error(
                    f"【SsdOffload】立刻运行一次：搬运 {h} 异常: {e}\n{traceback.format_exc()}"
                )
                failed += 1
                continue
            if res == "moved":
                moved += 1
            elif res == "failed":
                failed += 1
            else:
                skipped += 1

        summary = f"扫描 {total} / 已下发 {moved} / 跳过 {skipped} / 失败 {failed}"
        logger.info(f"【SsdOffload】立刻运行一次完成：{summary}")
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【SSD 卸载到 HDD】立刻运行一次",
                text=summary,
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

    def _get_source_service(self, event_downloader_name: Optional[str]):
        """取出源 qBittorrent 服务实例（搬运目前只支持 qb）。"""
        helper = self.downloader_helper or DownloaderHelper()

        # 优先按用户配置 / 事件给的下载器名称取
        candidate_names: List[str] = []
        if self._downloader_name:
            candidate_names.append(self._downloader_name)
        if event_downloader_name and event_downloader_name not in candidate_names:
            candidate_names.append(event_downloader_name)

        for name in candidate_names:
            try:
                svc = helper.get_service(name=name)
                if svc and self._get_service_type(svc) == "qbittorrent":
                    return svc
            except Exception as e:
                logger.debug(f"【SsdOffload】get_service({name}) 失败: {e}")

        # 兜底：扫一遍所有下载器，挑第一个 qb
        try:
            services = helper.get_services() or {}
            for svc in services.values():
                if self._get_service_type(svc) == "qbittorrent":
                    return svc
        except Exception as e:
            logger.debug(f"【SsdOffload】get_services 失败: {e}")
        return None

    def _get_downloader_options(self) -> List[Dict[str, str]]:
        """枚举 MP 中所有已启用的下载器，供下拉框使用，标题附带类型。"""
        options: List[Dict[str, str]] = []
        try:
            helper = self.downloader_helper or DownloaderHelper()
            services = helper.get_services() or {}
        except Exception as e:
            logger.warning(f"【SsdOffload】获取下载器列表失败: {e}")
            return options

        for name, svc in services.items():
            enabled = getattr(svc, "enabled", None)
            if enabled is None:
                config = getattr(svc, "config", None)
                if config is not None:
                    enabled = getattr(config, "enabled", True)
                else:
                    enabled = True
            if not enabled:
                continue
            svc_type = self._get_service_type(svc)
            title = f"{name}（{svc_type}）" if svc_type else name
            options.append({"title": title, "value": name})
        return options

    @staticmethod
    def _get_service_type(svc) -> str:
        try:
            t = getattr(svc, "type", "") or ""
            if t:
                return str(t).lower()
        except Exception:
            pass
        try:
            cls_name = type(getattr(svc, "instance", svc)).__name__.lower()
            for kw in ("qbittorrent", "transmission", "deluge", "rtorrent", "aria2"):
                if kw in cls_name:
                    return kw
        except Exception:
            pass
        return ""

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

    def stop_service(self):
        pass

    # ---------------------------------------------------------------------
    # UI 配置表单
    # ---------------------------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        downloader_options = self._get_downloader_options()
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
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
                                "props": {"cols": 12, "md": 3},
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
                                "props": {"cols": 12, "md": 3},
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "run_once",
                                            "label": "立刻运行一次",
                                            "color": "warning",
                                            "hint": "保存后立即扫一遍 qb 全量种子，符合条件的批量搬到 HDD；执行完自动重置",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "downloader_name",
                                            "label": "下载器1：源（运行下载任务）",
                                            "items": downloader_options,
                                            "clearable": True,
                                            "hint": "搬运仅支持 qBittorrent；非 qb 类型会被跳过并兜底挑第一个 qb",
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
                                            "model": "target_downloader_name",
                                            "label": "下载器2：目标（接管做种）",
                                            "items": downloader_options,
                                            "clearable": True,
                                            "hint": "标记将由哪个下载器接管做种（如 Transmission），用于后续闭环",
                                            "persistent-hint": True,
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
                                "props": {"cols": 12, "md": 6},
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
                                "工作原理：MP 上传 115 完成后触发 TransferComplete 事件，"
                                "对源 qBittorrent 调用 setLocation 把 SSD 上的种子数据搬到预配置的 HDD，"
                                "qb 后台 copy+delete 完成搬运、做种不掉。"
                                "「下载器2」仅作标识用（接管做种的下载器，例如 Transmission），"
                                "本插件不会主动操作它，由「自动转移做种」插件完成交接。"
                                "容器路径在 MP 与所有下载器中需完全一致。"
                            ),
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "dry_run": False,
            "run_once": False,
            "ssd_prefix": "/media/disk4-150G",
            "hdd_prefixes": "/media/disk1-8T\n/media/disk2-16T\n/media/disk3-16T",
            "strategy": "most_free",
            "downloader_name": "",
            "target_downloader_name": "",
            "required_tag": "",
            "delay_seconds": 5,
        }

    def get_page(self) -> List[dict]:
        return []
