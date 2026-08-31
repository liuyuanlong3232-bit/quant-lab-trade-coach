# 浏览器本地 OCR 依赖与隐私边界

- `tesseract.js` 6.x：Apache License 2.0，运行于浏览器 Web Worker。
- Tesseract `chi_sim` 与 `eng` traineddata：来自 Tesseract 官方语言数据，Apache License 2.0。

前端通过动态 `import()` 惰性加载 OCR 代码。首次识别时，运行库可能从其默认官方资源下载 WebAssembly worker 和简体中文/英文 traineddata；下载请求不包含用户图片。图片像素仅传给当前浏览器中的 OCR worker，不上传到 Quant-Lab、VPS 或第三方识别 API。OCR 文本只保存在 React 页面内存，刷新页面后消失，不写入浏览器存储。

网络不可用、模型下载失败或识别异常时，界面明确降级为人工填写；不会构造默认数值，也不会把行情现价、最新价或委托价当作成交价。
