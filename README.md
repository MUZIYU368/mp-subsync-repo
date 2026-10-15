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
├── tests/v3/                # 离线验证脚本（可脱离 MoviePilot 运行）
│   └── subsync/
│       ├── run_all.py           # 一键跑完全部验证
│       ├── harness.py           # 共用脚手架：装载插件 + 造目录树
│       ├── stub_host.py         # MoviePilot 宿主桩（apscheduler / app.*）
│       ├── verify_end_to_end.py # 核心功能端到端测试
│       └── verify_delete_source.py
├── icons/                   # 插件图标
│   └── subtitle.png
└── LICENSE
```

## 跑测试

```bash
python3 tests/v3/subsync/run_all.py
```

插件在 import 期依赖 `apscheduler` 与 `app.*`（MoviePilot 本体），
因此测试通过 `stub_host.py` 打桩后离线运行，不需要起 MP 容器。

**写新测试时注意两个坑**（都已踩过）：

1. **别用 `tempfile` 建目录**。系统临时目录叫 `tmp`，正好命中插件
   `STAGING_DIRS` 里的 `'tmp'`，`is_staging()` 会把挂在它下面的 strm
   全部当成 115 中转目录丢掉 —— 表现为「插件完全不工作」，
   很容易误判成插件有 bug。用 `harness.Tree`（建在 `tests/v3/subsync/.subsync_test_tmp/`）。
2. **先 `import stub_host; stub_host.install()`**，
   否则插件在第一行 import 就崩，什么都测不到。

## 维护约定

改动插件代码后，**必须同步更新三处版本号**，否则 MoviePilot 不会认为有新版本：

1. `plugins.v3/subsync/__init__.py` 里的 `plugin_version`
2. `package.v3.json` 里的 `version`
3. `package.v3.json` 里 `history` 的最新一条（key 形如 `v1.1.0`）

三者不一致时，用户端点「更新」没有任何反应。

## License

MIT，见 [LICENSE](LICENSE)。
