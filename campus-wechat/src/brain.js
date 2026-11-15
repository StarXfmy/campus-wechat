'use strict';

/**
 * 外部大脑：把消息转给远端 agent（飞书那颗大脑）拿回复。
 * 地址和 token 都走配置，本机联调填 127.0.0.1，上云只改 baseUrl。
 */

const DEFAULT_TIMEOUT = 20000;

class Brain {
  constructor(config, { logger } = {}) {
    const b = (config && config.brain) || {};
    this.enabled = b.enabled === true && Boolean(b.baseUrl);
    this.baseUrl = String(b.baseUrl || '').replace(/\/+$/, '');
    this.token = String(b.token || '');
    this.timeoutMs = Math.max(3000, Number(b.timeoutMs) || DEFAULT_TIMEOUT);
    this.logger = logger;
    this.stats = { ok: 0, fail: 0, lastOkAt: 0, lastError: '' };
  }

  async ask({ chatId, chatType, senderId, senderName, text, isMaster }) {
    if (!this.enabled) return null;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const started = Date.now();
    try {
      const res = await fetch(`${this.baseUrl}/chat`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(this.token ? { authorization: `Bearer ${this.token}` } : {}),
        },
        body: JSON.stringify({
          platform: 'wechat',
          chatId,
          chatType,
          senderId,
          senderName,
          text: String(text || ''),
          isMaster: Boolean(isMaster),
        }),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const reply = String((data && (data.reply || data.text)) || '').trim();
      if (!reply) throw new Error('远端返回空回复');
      this.stats.ok += 1;
      this.stats.lastOkAt = Date.now();
      if (this.logger) this.logger.info(`大脑回复 ${Date.now() - started}ms，${reply.length} 字`);
      return reply;
    } catch (err) {
      this.stats.fail += 1;
      this.stats.lastError = err && err.name === 'AbortError'
        ? `超时 ${this.timeoutMs}ms`
        : String((err && err.message) || err);
      if (this.logger) this.logger.warn(`大脑调用失败：${this.stats.lastError}`);
      return null;
    } finally {
      clearTimeout(timer);
    }
  }
}

module.exports = { Brain };
