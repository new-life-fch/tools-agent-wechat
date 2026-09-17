'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const DIR = path.join(os.homedir(), '.answer-panel');
const DEFAULT_FILE = path.join(DIR, 'bridge.json');
const STALE_MS = 7 * 24 * 3600 * 1000;

function isAlive(pid) {
  const n = Number(pid);
  if (!Number.isInteger(n) || n <= 0) return false;
  try {
    process.kill(n, 0);
    return true;
  } catch (err) {
    // EPERM = 进程存在但不属于我
    return !!(err && err.code === 'EPERM');
  }
}

function read(file) {
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    if (data && Array.isArray(data.instances)) return data;
  } catch (_) {
    /* 不存在 / 半截 / 坏 JSON 一律当空 */
  }
  return { version: 1, updated_at: 0, instances: [] };
}

function write(file, instances) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const payload = { version: 1, updated_at: Date.now(), instances };
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(payload, null, 2) + '\n', 'utf8');
  fs.renameSync(tmp, file);
  return payload;
}

function liveInstances(data) {
  const now = Date.now();
  return (data.instances || []).filter(
    (it) => it && it.port && isAlive(it.pid) && now - (Number(it.updated_at) || 0) < STALE_MS
  );
}

/** 登记本实例（同 pid 先剔除，避免重复） */
function register(file, self) {
  const data = read(file);
  const others = liveInstances(data).filter((it) => Number(it.pid) !== Number(self.pid));
  others.push(Object.assign({ updated_at: Date.now() }, self));
  return write(file, others);
}

/** 注销本实例；文件坏了也不抛 */
function unregister(file, pid) {
  try {
    const data = read(file);
    const rest = liveInstances(data).filter((it) => Number(it.pid) !== Number(pid));
    write(file, rest);
  } catch (_) {
    /* 退出路径上不报错 */
  }
}

module.exports = { DIR, DEFAULT_FILE, STALE_MS, isAlive, read, write, liveInstances, register, unregister };
