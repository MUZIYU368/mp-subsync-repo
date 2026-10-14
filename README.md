# MoviePilot 插件市场 · SubSync

个人维护的 MoviePilot 第三方插件市场仓库，结构与官方仓库
[jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins) 对齐。

## 安装

在 MoviePilot 中打开 **插件市场设置**，把下面的地址加入「插件仓库地址」，然后同步插件源：

```
https://github.com/MUZIYU368/mp-subsync-repo
```

多个地址用英文逗号分隔。仓库仅支持 GitHub 的 `main` 分支。

建议同时配置 `GITHUB_TOKEN`（docker-compose 的 `environment:` 或 `/config/app.env`），
否则访问 GitHub API 容易被限流，表现为同步插件源超时或更新卡住。

```yaml
environment:
  - PLUGIN_MARKET=https://github.com/MUZIYU368/mp-subsync-repo
  - GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

## 本仓库包含的插件

| 插件 | 说明 |
| --- | --- |
| [SubSync · 外挂字幕同步](plugins.v3/subsync/) | 整理完成后把下载目录的外挂字幕按 strm 同名复制到媒体库 |

## 目录结构

```
mp-subsync-repo/
├── package.v3.json          # 插件市场索引（V3）
├── plugins.v3/              # V3 插件源码
│   └── subsync/
│       ├── __init__.py
│       └── README.md
├── tests/v3/                # 离线验证脚本
│   └── subsync/
│       └── verify_delete_source.py
├── icons/                   # 插件图标
│   └── subtitle.png
└── LICENSE
```

## 维护约定

改动插件代码后，**必须同步更新三处版本号**，否则 MoviePilot 不会认为有新版本：

1. `plugins.v3/subsync/__init__.py` 里的 `plugin_version`
2. `package.v3.json` 里的 `version`
3. `package.v3.json` 里 `history` 的最新一条（key 形如 `v1.1.0`）

三者不一致时，用户端点「更新」没有任何反应。

## License

MIT，见 [LICENSE](LICENSE)。
