# 表情包素材目录（需自备）

运行时的表情包放在 `data/stickers/`，按分类建子目录：

```
data/stickers/
├── 惊讶/
│   ├── a.png
│   └── b.jpg
├── 加油/
│   └── c.gif
├── labels.json       # 自动生成，标签索引
└── watermarks.json   # 自动生成，水印记录
```

**本项目不附带任何第三方表情包素材**——网上存的图有版权，不能随代码分发。
`data/` 已在 `.gitignore` 里排除，你放的图不会被提交。

想让她会发表情包：自己往分类目录里放图，`src/stickers.js` 会扫描建索引，
分类名用描述性中文（「惊讶」「加油」「猫」），她按当前对话情绪挑。

不想用就把 `config.json` 里的 `chat.stickerChance` 设为 0，她只发文字。
