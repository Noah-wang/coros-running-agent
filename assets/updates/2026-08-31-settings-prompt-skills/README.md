# 2026-08-31 · 设置与 Prompt Skills

这次更新的全部图片素材。

## 更新内容

- **设置与 Prompt Skills 控制台**（`/settings`）：自动化开关、令牌保护、免重启生效
- **可上传的教练提示词**：Markdown 直接生效，也可一键恢复内置
- **中英双语切换**：界面、后端文案、Agent 回答三层一起切

## 文件

| 文件 | 尺寸 | 用途 |
| --- | --- | --- |
| `cover-xiaohongshu.svg` | 3:4 矢量 | 小红书封面源文件，可无损改字改色 |
| `cover-xiaohongshu.png` | 1080×1440 | 小红书封面成品，平台推荐尺寸 |
| `settings-zh.png` | 2560×3004 | 设置页中文版，已解锁的完整状态 |
| `settings-en.png` | 2560×2990 | 设置页英文版 |
| `chat-zh.png` | 2560×1720 | 对话页，展示导航新增的「设置」入口和语言按钮 |

截图用 `demo-token` 生成，**不是真实令牌**；示例实例无个人数据。

## 怎么重新生成

封面改文案后重新导出：

```bash
playwright screenshot --viewport-size=1080,1440 \
  "file://$PWD/cover-xiaohongshu.svg" cover-xiaohongshu.png
```

SVG 只带 `viewBox`、不带 `width`/`height`，所以浏览器会自动铺满视口——
换个 `--viewport-size` 就能出任意分辨率，不用改文件。

页面截图见仓库根目录的 `scripts/`，或用 Playwright 驱动：设置页要先往
`sessionStorage` 写 `coros-settings-token`，`settings.js` 加载时看到令牌会自动解锁。

## 设计配方

封面用 [mono-color](https://github.com/yanliudesign/mono-color-skill) 的设计系统：

- 纸面 Cool Gray `#E9E9E5`（catalog 标注用于 technology / charcoal-led systems）
- 双色 Charcoal `#30343A` + Signal Red `#C83232`，红色只用在焦点和分隔
- 版式 `composition_ruled_information`（产品更新属于 factual announcement）
- 字体角色 `type_programmatic`，它明确支持 bilingual-safe spacing

**没有用栅格模型生成。** 中文标题交给图像模型容易糊，而「高驰Coros Agent」
是必须原样保留的字，所以改用 SVG 精确排版。
