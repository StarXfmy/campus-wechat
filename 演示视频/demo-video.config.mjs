/**
 * 校园木鱼 · 微信端 演示视频
 *
 * 画面源是一段连续录屏（微信木鱼.mp4），每幕取源视频的一个区间。
 * 插件流水线负责配音：demo-video tts → audio/<id>.mp3 + <id>.vtt + durations.json
 * 合成由 build_video.py 完成（切幕 + 裁窗放大 + 烧字幕 + 对齐音频时长 + 拼接），
 * 因为插件 compose 不烧字幕。每幕的源视频区间写在 build_video.py 的 SCENES 里。
 */
export default {
  name: '校园木鱼微信端演示',
  finalVideo: '校园木鱼微信端.mp4',
  viewport: { width: 1920, height: 1080 },
  fps: 30,

  tts: {
    voice: 'zh-CN-YunxiNeural',
    rate: '-4%',
    // 不用 fixes：等长替换是硬约束，英文缩写一律在文稿里改成中文口播
    fixes: [],
  },

  source: {
    video: 'C:/Users/lenovo/Desktop/微信木鱼.mp4',
    // 微信窗口在 2558×1598 画面里的位置，裁出来放大到 1920×1080，底部留字幕带
    crop: { x: 450, y: 254, w: 1470, h: 1004 },
  },

  scenes: [
    { id: 'm00', title: '片头', card: 'title' },
    { id: 'm01', title: '三进程架构', src: [1.0, 27.0] },
    { id: 'm02', title: '打个招呼', src: [27.0, 52.0] },
    { id: 'm03', title: '消费与课表', src: [52.0, 75.0] },
    { id: 'm04', title: '二课活动', src: [75.0, 101.5] },
    { id: 'm05', title: '知识库带出处', src: [101.5, 125.0] },
    { id: 'm06', title: '不知道就去搜', src: [133.0, 162.0] },
    { id: 'm07', title: '发邮件要确认', src: [162.0, 188.0] },
    { id: 'm08', title: '语音与诚实', src: [188.0, 226.0] },
    { id: 'm09', title: '群聊少说话', src: [244.5, 275.0] },
    { id: 'm10', title: '看图', src: [275.0, 297.0] },
    { id: 'm11', title: '片尾', card: 'outro' },
  ],
};
