# MoviePilot-Plugins

个人 MoviePilot V2 插件仓库。

## 安装方法

打开 MoviePilot → **设定 → 插件 → 插件市场仓库地址**，添加：

```
https://github.com/Cyrker/MoviePilot-Plugins/
```

多个地址用 `,` 分隔。保存后回到 **插件 → 插件市场**，刷新即可看到本仓库的插件。

## 插件列表

| 名称 | 版本 | 说明 |
| --- | --- | --- |
| [SSD 卸载到 HDD](plugins.v2/ssdoffload/) | v1.0.0 | 整理完成后自动把 qb 中位于 SSD 缓存盘的种子搬到机械盘，搬运由 qb setLocation 完成，做种不掉。配合自动转移做种插件可实现「下载到SSD → 整理上传网盘 → 搬运到HDD → 转交TR保种」全自动闭环。 |

## 开发规范

本仓库结构遵循 [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) 官方规范：

- `plugins.v2/<plugin_id>/` 存放 V2 插件源码，目录名必须为插件类名小写
- `package.v2.json` 仓库根目录的 V2 插件市场清单
- `icons/` 存放插件图标
- 修改插件代码后必须同步修改 `package.v2.json` 中对应的 `version` 字段，并在 `history` 中追加版本说明，MoviePilot 才会提示用户更新

## License

MIT
