# 表情包放这儿

这个目录**故意是空的** —— 里面的图是从网上收集的梗图，版权不属于本项目，所以仓库里不带。

想让她会发表情包，往这儿放你自己的图（png / jpg / gif / webp，文件名随便），然后二选一：

1. **什么都不用做**：`fishbot/stickers.py` 没有 `manifest.json` 时会扫目录，**拿文件名当标签**。
   比如 `happy.png` 就能被 `[[表情:happy]]` 选中，`angry_cat.gif` 的标签是 `angry` 和 `cat`。
2. **想要更准**：自己写一份 `manifest.json`，格式如下：

```json
[
  {"file": "happy.png", "tags": ["开心", "笑"], "desc": "笑得直拍尾巴"},
  {"file": "goutou.png", "tags": ["狗头", "保命"], "desc": "狗头保命"}
]
```

配好后在 `config.yaml` 里保持：

```yaml
stickers:
  enabled: true
  dir: assets/stickers
  manifest: manifest.json
```

运行日志里会打印「表情包：加载 N 张（M 个标签）」，看到就是成了。
