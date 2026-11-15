'use strict';

/**
 * 表情包：按「这张图表达什么」来挑，不是随机抽。
 *
 * 标签来自 data/stickers/labels.json，格式 "情绪|适用场景"，
 * 由 tools/describe-stickers.cjs 用视觉模型一次性生成。
 * 匹配不上就不发 —— 宁可没有表情，也别在不合适的时候甩一张。
 */
const fs = require('node:fs');
const path = require('node:path');
const { projectRoot } = require('./config');

let cache = { mtime: 0, items: [] };

function load() {
  const root = projectRoot();
  const file = path.join(root, 'data', 'stickers', 'labels.json');
  let st;
  try {
    st = fs.statSync(file);
  } catch {
    return [];
  }
  if (st.mtimeMs === cache.mtime) return cache.items;

  const items = [];
  try {
    const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
    for (const [rel, label] of Object.entries(raw || {})) {
      const text = String(label || '').trim();
      if (!text) continue;
      const [feeling, scene] = text.split('|');
      const abs = path.join(root, 'data', 'stickers', rel);
      if (!fs.existsSync(abs)) continue;
      items.push({
        rel,
        abs,
        feeling: String(feeling || '').trim(),
        scene: String(scene || '').trim(),
      });
    }
  } catch {
    return cache.items;
  }
  cache = { mtime: st.mtimeMs, items };
  return items;
}

// 回复里出现这些 → 想要哪种情绪。顺序从具体到宽泛，先命中的说了算。
const RULES = [
  // 吃的放最前：聊食堂时「好香」比「开心」更贴
  { when: /(好吃|真香|好香|馋|想吃|饿了|没吃饱|食堂)/, want: /(香|饿|吃|馋)/ },
  { when: /(晚安|睡觉|睡了|早点休息)/, want: /(睡|哄睡|晚安)/ },
  { when: /(哈哈|笑死|笑喷|太好笑|乐死|绷不住)/, want: /(笑|开心|狂笑|欢呼|兴奋|逗乐)/ },
  { when: /(好累|累了|困|想睡|摆烂|躺平|不想动|没精神|不想干)/, want: /(累|瘫|躺|摆烂|休息)/ },
  { when: /(生气|气死|烦死|过分|离谱|烦)/, want: /(生气|无语|劝|发火)/ },
  { when: /(谢谢|感谢|抱抱|贴贴|爱你|喜欢你|真好|开心)/, want: /(喜欢|抱抱|贴贴|撒娇|亲昵|依偎|卖萌)/ },
  { when: /(对不起|抱歉|我错|不好意思|闯祸)/, want: /(道歉|求饶|委屈|抱歉)/ },
  { when: /(什么情况|怎么会|不可能|震惊|居然|没想到|惊了)/, want: /(惊恐|惊讶|震惊|惊叹)/ },
  { when: /(我想想|琢磨|考虑|思考|怎么办才好|容我想)/, want: /(思考|发呆|若有所思)/ },
];

/** 不带情绪匹配，随便给一张。只在用户点名要表情时用。 */
function random() {
  const items = load();
  if (!items.length) return null;
  return items[Math.floor(Math.random() * items.length)];
}

function pick(text) {
  const items = load();
  if (!items.length) return null;
  const t = String(text || '');
  for (const rule of RULES) {
    if (!rule.when.test(t)) continue;
    const hits = items.filter((it) => rule.want.test(it.feeling) || rule.want.test(it.scene));
    if (hits.length) return hits[Math.floor(Math.random() * hits.length)];
    return null;
  }
  return null;
}

module.exports = { load, pick, random };
