'use strict';

const DEFAULT_LIMIT = 200;

/**
 * 面板内容仓库：一个有序的消息列表，消息可带 id（同 id 再推 = 就地更新）。
 * 只做数据，不碰 VSCode API，方便单独测试。
 */
class Store {
  constructor(limit) {
    this.limit = Number.isFinite(limit) && limit > 0 ? Math.floor(limit) : DEFAULT_LIMIT;
    this.items = [];
    this.seq = 0;
    this._listeners = new Set();
  }

  onDidChange(fn) {
    this._listeners.add(fn);
    return () => this._listeners.delete(fn);
  }

  _emit(ev) {
    for (const fn of Array.from(this._listeners)) {
      try {
        fn(ev);
      } catch (_) {
        /* 监听者自身出错不影响仓库 */
      }
    }
  }

  push(input) {
    const src = input || {};
    const now = Date.now();
    const text = src.text == null ? '' : String(src.text);

    let item = src.id ? this.items.find((it) => it.id === String(src.id)) : null;
    if (item) {
      if (src.title != null && String(src.title) !== '') item.title = String(src.title);
      item.text = text;
      item.updatedAt = now;
      item.seq = ++this.seq;
      const trimmed = this._evict();
      if (trimmed) this._emit({ type: 'reset' });
      else this._emit({ type: 'update', item });
      return item;
    }

    item = {
      id: src.id == null || src.id === '' ? undefined : String(src.id),
      title: src.title == null ? '' : String(src.title),
      source: src.source == null ? '' : String(src.source),
      text,
      createdAt: now,
      updatedAt: now,
      seq: ++this.seq
    };
    this.items.push(item);
    const trimmed = this._evict();
    if (trimmed) this._emit({ type: 'reset' });
    else this._emit({ type: 'append', item });
    return item;
  }

  _evict() {
    let n = 0;
    while (this.items.length > this.limit) {
      this.items.shift();
      n += 1;
    }
    return n;
  }

  clear() {
    if (this.items.length === 0) return 0;
    const n = this.items.length;
    this.items = [];
    this._emit({ type: 'clear' });
    return n;
  }

  list() {
    return this.items;
  }

  count() {
    return this.items.length;
  }

  /** 面板里「复制全部」用的纯文本 */
  toPlainText() {
    return this.items
      .map((it) => {
        const head = it.title ? `【${it.title}】\n` : '';
        return head + it.text;
      })
      .join('\n\n' + '-'.repeat(24) + '\n\n');
  }

  snapshot() {
    return { seq: this.seq, items: this.items };
  }

  restore(data) {
    if (!data || !Array.isArray(data.items)) return;
    this.items = data.items
      .filter((it) => it && typeof it.text === 'string')
      .slice(-this.limit)
      .map((it) => ({
        id: it.id == null ? undefined : String(it.id),
        title: it.title == null ? '' : String(it.title),
        source: it.source == null ? '' : String(it.source),
        text: it.text,
        createdAt: Number(it.createdAt) || Date.now(),
        updatedAt: Number(it.updatedAt) || Date.now(),
        seq: Number(it.seq) || 0
      }));
    const maxSeq = this.items.reduce((m, it) => Math.max(m, it.seq), 0);
    this.seq = Number(data.seq) > maxSeq ? Number(data.seq) : maxSeq;
  }
}

module.exports = { Store, DEFAULT_LIMIT };
