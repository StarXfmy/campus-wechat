'use strict';

const { execFile } = require('child_process');

const CLI = process.env.HDUHELP_CLI || 'C:\\path\\to\\hduhelp-cli.exe';

// buildingID → 学生平时的叫法
const BUILDING_LABEL = {
  '01': '第1教研楼',
  '02': '第2教研楼',
  '03': '第3教研楼',
  '04': '第4教研楼',
  '06': '第6教研楼',
  '07': '第7教研楼',
  '08': '第8教研楼',
  '09': '第9教研楼',
  '10': '第10教研楼',
  '11': '第11教研楼',
  '12': '第12教研楼',
  '6n': '第6教研楼南',
  '6s': '第6教研楼北',
  D: 'D楼',
  '3xj': '新教',
  '3xd': '新东教',
  '3d': '电二电三',
  ZX: '正心楼',
  CZ: '创智楼',
  QY: '青越',
  XY: '芯越楼',
  JC: '精诚楼',
  '2by': '博约楼',
  '2lx': '礼贤楼',
  '2zj': '子衿楼',
  YC: '第一餐厅形体房',
  kj: '科技馆',
  xh: '学生活动中心',
  sy: '电子线路实验室',
  JY: '竞越体育',
  SXXN: '绍兴校内',
  SXTY: '绍兴田径场',
};

// 用户确认的绍兴楼栋：正心、创智、青越、芯越、精诚；其余楼栋接口无校区字段，不硬分类
const SHAOXING = new Set(['ZX', 'CZ', 'QY', 'XY', 'JC']);

// 9999 是「待定/校际选修课」、ts 是「上课通知」，都不是真实教室
const SKIP = new Set(['9999', 'ts']);

const QUERY_RE = /(空教室|空课室|空的教室|有(没有)?空(教室|位|房)|哪里(能|可以)?自习|去哪(儿)?自习|哪个教室(空|没人|有位置)|自习室)/;

function isQuery(text) {
  return QUERY_RE.test(String(text || ''));
}

function detectBuilding(text) {
  const t = String(text || '').toUpperCase().replace(/\s+/g, '');
  if (/正心/.test(t)) return 'ZX';
  if (/创智/.test(t)) return 'CZ';
  if (/青越/.test(t)) return 'QY';
  if (/芯越/.test(t)) return 'XY';
  if (/精诚/.test(t)) return 'JC';
  if (/博约/.test(t)) return '2by';
  if (/礼贤/.test(t)) return '2lx';
  if (/子衿/.test(t)) return '2zj';
  if (/(^|[^0-9A-Z])D(楼|座)/.test(t)) return 'D';
  if (/6教.*南|第?6教研楼南/.test(t)) return '6n';
  if (/6教.*北|第?6教研楼北/.test(t)) return '6s';
  const m = t.match(/第?(\d{1,2})教(研楼)?/);
  if (m) {
    const id = m[1].padStart(2, '0');
    if (BUILDING_LABEL[id]) return id;
  }
  return null;
}

function wantsShaoxing(text) {
  return /绍兴/.test(String(text || ''));
}

function parseFirstJson(raw) {
  const s = String(raw || '');
  const start = s.indexOf('{');
  if (start < 0) return null;
  for (let end = s.lastIndexOf('}'); end > start; end = s.lastIndexOf('}', end - 1)) {
    try {
      return JSON.parse(s.slice(start, end + 1));
    } catch { /* 尾巴上可能跟着更新提示，继续缩短 */ }
  }
  return null;
}

function runCli(args, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    execFile(CLI, args, {
      timeout: timeoutMs,
      windowsHide: true,
      maxBuffer: 8 * 1024 * 1024,
      encoding: 'utf8',
    }, (err, stdout) => {
      const obj = parseFirstJson(stdout);
      if (obj) return resolve(obj);
      if (err) return reject(err);
      reject(new Error('CLI 没有返回可解析的 JSON'));
    });
  });
}

function label(id) {
  const key = String(id || '');
  return BUILDING_LABEL[key] || BUILDING_LABEL[key.toUpperCase()] || key;
}

function roomLabel(buildingId, name) {
  const n = String(name || '').replace(/[（(][^）)]*[）)]/g, '').trim();
  if (!n) return '';
  if (/楼|馆|厅|室|场|中心/.test(n)) return n;
  return label(buildingId) + n;
}

async function listEmpty({ buildingId, limit = 200 } = {}) {
  const rows = [];
  let total = 0;
  let offset = 0;
  for (let page = 0; page < 8; page += 1) {
    const args = ['academic', 'classroom', '--status', 'unused', '--limit', String(limit), '--offset', String(offset), '--json'];
    if (buildingId) args.push('--building-id', buildingId);
    const obj = await runCli(args);
    if (!obj || obj.code !== 0) throw new Error((obj && obj.msg) || '空教室接口返回异常');
    const batch = (obj.data || []).filter((r) => r && r.available !== false && !SKIP.has(r.buildingID));
    rows.push(...batch);
    total = (obj.pagination && obj.pagination.total) || rows.length;
    if (!obj.pagination || !obj.pagination.hasMore) break;
    offset = obj.pagination.nextOffset;
  }
  return { total, rows };
}

function groupByBuilding(rows) {
  const by = new Map();
  for (const r of rows) {
    const id = r.buildingID;
    if (!by.has(id)) by.set(id, { id, count: 0, rooms: [] });
    const b = by.get(id);
    b.count += 1;
    if (b.rooms.length < 3) {
      const name = roomLabel(id, r.locationName);
      if (name) b.rooms.push(name);
    }
  }
  return [...by.values()].sort((a, b) => b.count - a.count);
}

async function answer(text) {
  const buildingId = detectBuilding(text);

  // 指定楼栋
  if (buildingId) {
    const { total, rows } = await listEmpty({ buildingId });
    if (!rows.length) return `${label(buildingId)}现在没有查到空教室`;
    const rooms = rows.slice(0, 5).map((r) => roomLabel(r.buildingID, r.locationName)).filter(Boolean);
    return `${label(buildingId)}现在有 ${total} 间空的：${rooms.join('、')}${total > rooms.length ? ' 等' : ''}`;
  }

  const { total, rows } = await listEmpty({});
  if (!rows.length) return '现在没查到空教室';

  const sx = rows.filter((r) => SHAOXING.has(r.buildingID));
  const xs = rows.filter((r) => !SHAOXING.has(r.buildingID));

  // 只问绍兴
  if (wantsShaoxing(text)) {
    if (!sx.length) return '绍兴校区现在没查到空教室';
    const top = groupByBuilding(sx).slice(0, 4).map((b) => `${label(b.id)}${b.count}间`).join('、');
    return `绍兴校区现在 ${sx.length} 间空的，${top}`;
  }

  // 不指定：两个校区都报
  const sxTop = groupByBuilding(sx).slice(0, 2).map((b) => `${label(b.id)}${b.count}间`).join('、');
  const xsTop = groupByBuilding(xs).slice(0, 2).map((b) => `${label(b.id)}${b.count}间`).join('、');
  const parts = [`下沙 ${xs.length} 间`, `绍兴 ${sx.length} 间`];
  const detail = [xsTop, sxTop].filter(Boolean).join('；');
  return `现在空教室：${parts.join('、')}${detail ? `。${detail}` : ''}${total ? '' : ''}`;
}

module.exports = { isQuery, detectBuilding, listEmpty, answer, BUILDING_LABEL, SHAOXING };
