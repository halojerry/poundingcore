#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
from datetime import datetime
from pathlib import Path
from typing import Any

import socket as _socket

from scripts._const import DATA_DIR, DEFAULT_CACHE_TTL_SECONDS, get_config_profile

from scripts.lib.cdp_client import CdpConnection, CdpTab

# Backward compat aliases for except blocks
PlaywrightError = Exception
PlaywrightTimeoutError = TimeoutError


def _pick_free_port() -> int:
    """Find a free TCP port on localhost."""
    sock = _socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def sleep_random(min_ms: int, max_ms: int) -> None:
    """随机延迟，模拟人类行为"""
    delay = random.randint(min_ms, max_ms) / 1000.0
    time.sleep(delay)


def navigation_delay() -> None:
    """页面导航后的随机等待，模拟人类阅读"""
    sleep_random(800, 2000)


_CACHE_TTL = DEFAULT_CACHE_TTL_SECONDS  # 24h — reuse cached probe results within this window
from scripts._errors import ConfigError, ValidationError

# Module-level flag to prevent concurrent login waits
import threading as _threading
_login_in_progress: bool = False
_login_lock = _threading.Lock()
_login_result: dict[str, Any] | None = None
_login_done_event = _threading.Event()
from scripts.lib.reference_images import is_likely_product_image
from scripts.lib.task_paths import current_task_id, task_media_dir


EXTRACT_1688_JS = r"""
(() => {
  // Trigger lazy-load DOM by scrolling
  try {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'instant' });
    window.scrollTo({ top: 0, behavior: 'instant' });
    window.scrollTo({ top: document.body.scrollHeight / 2, behavior: 'instant' });
  } catch(e) {}

  const normalizeText = (value) => {
    if (value == null) return null;
    const text = String(value).replace(/\s+/g, ' ').trim();
    return text || null;
  };
  const cleanUrl = (value) => {
    if (!value) return null;
    try {
      return new URL(value, location.href).toString();
    } catch {
      return normalizeText(value);
    }
  };
  const cleanImageUrl = (value) => {
    const url = cleanUrl(value);
    if (!url) return null;
    return url
      .replace(/\.webp$/i, '.jpg')
      .replace(/\.jpg_(sum|b)\.jpg$/i, '.jpg')
      .replace(/_(sum|b)\.jpg$/i, '.jpg')
      .replace(/_\d+x\d+\.jpg$/i, '.jpg')
      .replace(/_\d+q\d+\.jpg$/i, '.jpg')
      .replace(/\.jpg\.jpg$/i, '.jpg')
      .replace(/\.jpg_\.jpg$/i, '.jpg')
      .replace(/_88x88q90/i, '');
  };
  const dedupe = (items) => {
    const out = [];
    const seen = new Set();
    for (const item of items || []) {
      const key = typeof item === 'string' ? item : JSON.stringify(item, Object.keys(item || {}).sort());
      if (!key || seen.has(key)) continue;
      seen.add(key);
      out.push(item);
    }
    return out;
  };
  const queryAll = (selectors, root = document) => {
    const out = [];
    for (const selector of selectors) {
      try { root.querySelectorAll(selector).forEach((el) => out.push(el)); } catch {}
    }
    return out;
  };
  const pickText = (selectors, root = document) => {
    for (const selector of selectors) {
      try {
        const el = root.querySelector(selector);
        const text = normalizeText(el?.innerText || el?.textContent);
        if (text) return text;
      } catch {}
    }
    return null;
  };
  const pickTextFrom = (root, selectors) => {
    for (const selector of selectors) {
      try {
        const el = root?.querySelector?.(selector);
        const text = normalizeText(el?.innerText || el?.textContent);
        if (text) return text;
      } catch {}
    }
    return null;
  };
  const pickAttr = (selectors, attr, root = document) => {
    for (const selector of selectors) {
      try {
        const el = root.querySelector(selector);
        const val = el?.getAttribute?.(attr) || el?.[attr];
        if (val) return val;
      } catch {}
    }
    return null;
  };
  const pickAttrFrom = (root, selectors, attr) => {
    for (const selector of selectors) {
      try {
        const el = root?.querySelector?.(selector);
        const val = el?.getAttribute?.(attr) || el?.[attr];
        if (val) return val;
      } catch {}
    }
    return null;
  };
  const parseNumber = (value) => {
    if (value == null) return null;
    const text = String(value).replace(/,/g, '.');
    const match = text.match(/-?\d+(?:\.\d+)?/);
    return match ? Number(match[0]) : null;
  };
  const parseInteger = (value) => {
    if (value == null) return null;
    const digits = String(value).replace(/[^\d]/g, '');
    return digits ? Number(digits) : null;
  };
  const readImages = (selectors, root = document, limit = 100) => {
    const items = [];
    // 非产品图的 DOM 容器（评价区头像、交易信息等）
    const NON_PRODUCT_SELECTORS = [
      '.ant-avatar', '.user-avatar', '.avatar', '.avatar-img',
      '.evaluation-list', '.evaluation-content', '.comment-list', '.comment-item',
      '.trade-info', '.user-tag', '.member-info',
      '.footer', '.header', '.nav', '.sidebar', '.recommend', '.related-products',
      '.od-sidebar', '.offer-sidebar',
      '[class*="avatar"]', '[class*="Avatar"]',
    ];
    const isNonProductArea = (el) => {
      for (const sel of NON_PRODUCT_SELECTORS) {
        if (el.closest(sel)) return true;
      }
      return false;
    };
    queryAll(selectors, root).forEach((el) => {
      // DOM 上下文过滤：跳过非产品区域（头像、评价、导航等）
      if (isNonProductArea(el)) return;
      const src = cleanImageUrl(
        el?.currentSrc || el?.src || el?.getAttribute?.('src') || el?.getAttribute?.('data-src') || el?.getAttribute?.('data-lazy-src') || el?.getAttribute?.('data-lazyload-src') || el?.getAttribute?.('data-original')
      );
      if (!src) return;
      if (/data:image|placeholder|icon|logo|sprite|loading|\.svg/i.test(src)) return;
      if (/!!0-0-|_88x88|_24x24|_48x48/i.test(src)) return;
      items.push(src);
    });
    return dedupe(items).slice(0, limit);
  };
  const readResourceImages = (limit = 150) => {
    const items = [];
    // 只接受已知产品图 CDN 域名
    const PRODUCT_IMG_CDN = /cbu01\.alicdn\.com\/img\/ibank|img\.alicdn\.com\/img\/ibank|cbu01\.alicdn\.com\/img\/offer/i;
    try {
      const resources = performance.getEntriesByType('resource') || [];
      resources.forEach((entry) => {
        const src = cleanImageUrl(entry?.name);
        if (!src) return;
        if (!PRODUCT_IMG_CDN.test(src)) return;
        if (/data:image|placeholder|icon|logo|sprite|loading|!!0-0-|_88x88|_24x24|_48x48|svg/i.test(src)) return;
        items.push(src);
      });
    } catch {}
    return dedupe(items).slice(0, limit);
  };
  const readPairsBySelectors = (rowSelectors, keySelectors, valueSelectors, root = document, limit = 100) => {
    const pairs = [];
    queryAll(rowSelectors, root).forEach((row) => {
      const name = pickTextFrom(row, keySelectors);
      const value = pickTextFrom(row, valueSelectors);
      if (name && value && name !== value && value.length < 500) pairs.push({ name, value });
    });
    return dedupe(pairs).slice(0, limit);
  };
  const readAntDescriptionsPairs = (root = document, limit = 100) => {
    const pairs = [];
    queryAll(['.module-od-product-attributes .ant-descriptions-row', '#productAttributes .ant-descriptions-row', '.ant-descriptions-row'], root).forEach((row) => {
      const labels = Array.from(row.querySelectorAll('.ant-descriptions-item-label')).map((cell) => normalizeText(cell.innerText || cell.textContent));
      const values = Array.from(row.querySelectorAll('.ant-descriptions-item-content')).map((cell) => normalizeText(cell.innerText || cell.textContent));
      const size = Math.min(labels.length, values.length);
      for (let index = 0; index < size; index += 1) {
        const name = labels[index];
        const value = values[index];
        if (name && value && name !== value && value.length < 500) {
          pairs.push({ name, value });
        }
      }
    });
    return dedupe(pairs).slice(0, limit);
  };
  const readDescriptionBlock = (selectors) => {
    for (const selector of selectors) {
      try {
        const el = document.querySelector(selector);
        const text = normalizeText(el?.innerText || el?.textContent);
        if (text && text.length > 20) return text;
      } catch {}
    }
    return null;
  };
  const findReactFiber = (node) => {
    if (!node) return null;
    const names = [];
    try { names.push(...Object.getOwnPropertyNames(node)); } catch {}
    try { names.push(...Object.keys(node)); } catch {}
    for (const key of names) {
      if (key.startsWith('__reactFiber') || key.startsWith('__reactInternalInstance')) {
        try { return node[key]; } catch {}
      }
    }
    return null;
  };
  const getWindowInitData = () => {
    try { return globalThis.__INIT_DATA__ || window.__INIT_DATA__ || null; } catch { return null; }
  };
  const safeJsonSample = (value, limit = 4000) => {
    if (value == null) return null;
    try {
      const json = JSON.stringify(value);
      if (!json) return null;
      return json.length > limit ? json.slice(0, limit) : json;
    } catch {
      return null;
    }
  };
  const deepFindDimensionLike = (value, path = [], depth = 0, out = []) => {
    if (value == null || depth > 4 || out.length >= 40) return out;
    if (typeof value === 'string') {
      const keyPath = path.join('.').toLowerCase();
      const raw = value.trim();
      if (!raw) return out;
      if (/(尺寸|规格|长|宽|高|直径|口径|size|spec|dimension|length|width|height|diameter)/i.test(keyPath) || /\d+(?:\.\d+)?\s*(cm|mm|厘米|毫米)/i.test(raw)) {
        out.push({ path: path.join('.'), value: raw });
      }
      return out;
    }
    if (typeof value === 'number' || typeof value === 'boolean') return out;
    if (Array.isArray(value)) {
      value.slice(0, 20).forEach((item, index) => deepFindDimensionLike(item, path.concat(String(index)), depth + 1, out));
      return out;
    }
    if (typeof value === 'object') {
      Object.entries(value).slice(0, 40).forEach(([key, item]) => {
        deepFindDimensionLike(item, path.concat(String(key)), depth + 1, out);
      });
    }
    return out;
  };
  const collectPageStructuredData = () => {
    const candidates = [];
    const addCandidate = (name, value) => {
      if (value == null) return;
      const sample = safeJsonSample(value);
      const dimensionHints = deepFindDimensionLike(value, [name]);
      const keys = (value && typeof value === 'object' && !Array.isArray(value)) ? Object.keys(value).slice(0, 20) : [];
      if (sample || dimensionHints.length || keys.length) {
        candidates.push({ name, keys, sample, dimensionHints });
      }
    };
    addCandidate('__INIT_DATA__', getWindowInitData());
    try { addCandidate('globalData', window.globalData || globalThis.globalData || null); } catch {}
    try { addCandidate('__PRELOADED_STATE__', window.__PRELOADED_STATE__ || globalThis.__PRELOADED_STATE__ || null); } catch {}
    try { addCandidate('__NUXT__', window.__NUXT__ || globalThis.__NUXT__ || null); } catch {}
    return candidates.slice(0, 10);
  };
  const extractRuntimeSkuData = () => {
    const roots = [
      document.querySelector('.pc-sku-wrapper'),
      document.querySelector('.gyp-sku-selection-order-button-wrap'),
      document.querySelector('.pc-sku-gyp-more-dimension-wrapper'),
      document.querySelector('.cart-sider'),
    ].filter(Boolean);
    const fromPanel = (fiber) => {
      const props = fiber?.child?.memoizedProps || fiber?.memoizedProps || {};
      const panel = props?.skuPannelInfo;
      if (!panel || !panel.getData || !panel.getSubmitData) return null;
      const selected = panel.getSelected?.()?.selectedSku || {};
      const imageMap = {};
      const rawSkuProps = panel?._state?.skuProps || panel.getData?.()?.skuProps || [];
      if (Array.isArray(rawSkuProps)) {
        rawSkuProps.forEach((group) => {
          (group?.value || []).forEach((item) => {
            if (item?.name && item?.imageUrl) imageMap[item.name] = cleanImageUrl(item.imageUrl);
          });
        });
      }
      const data = panel.getData() || {};
      const skuSpecIdMap = data.skuSpecIdMap || {};
      const submit = panel.getSubmitData() || {};
      const priceRanges = Array.isArray(data.priceRanges) ? data.priceRanges : [];
      const totalQty = Array.isArray(submit.submitData) ? submit.submitData.reduce((sum, row) => sum + Number(row?.quantity || 0), 0) : 0;
      let activePrice = null;
      for (const range of priceRanges) {
        if (totalQty >= Number(range?.beginAmount || 0)) activePrice = range?.price ?? activePrice;
      }
      let sku = Object.keys(selected).filter((key) => Number(selected[key]) > 0).map((key) => {
        const item = skuSpecIdMap[key] || {};
        return {
          specId: item.specId || key,
          skuId: item.skuId || item.specId || key,
          name: normalizeText(item.specAttrs || item.name || key),
          specAttrs: normalizeText(item.specAttrs || item.name || key),
          canBookCount: item.canBookCount ?? null,
          skuCount: Number(selected[key] || 0),
          firstProp: normalizeText(item.firstProp || null),
          image: cleanImageUrl(imageMap[item.firstProp]) || null,
          price: parseNumber(item.discountPrice ?? item.price ?? activePrice),
          discountPrice: parseNumber(item.discountPrice),
        };
      });
      if ((!sku || sku.length === 0) && Array.isArray(submit.submitData)) {
        sku = submit.submitData.map((row, index) => ({
          specId: row?.specId || `submit-${index + 1}`,
          skuId: row?.skuId || row?.specId || `submit-${index + 1}`,
          name: normalizeText(row?.specAttrs || row?.name || `规格${index + 1}`),
          specAttrs: normalizeText(row?.specAttrs || row?.name || `规格${index + 1}`),
          canBookCount: row?.canBookCount ?? null,
          skuCount: Number(row?.quantity || 0),
          firstProp: null,
          image: null,
          price: parseNumber(activePrice),
          discountPrice: null,
        }));
      }
      return { success: submit.success !== false, message: normalizeText(submit.message) || null, selectedSkuMap: selected, priceRanges, imageMap, sku };
    };
    const fromMoreDimension = (fiber) => {
      try {
        const orderList = fiber?.memoizedProps?.children?.[1]?.props?.orderList || [];
        const sku = orderList.map((row) => ({
          specId: row?.props?.specId || null,
          skuId: row?.props?.skuId || row?.props?.specId || null,
          name: normalizeText((row?.props?.map || []).map((item) => item?.value).join('-')),
          specAttrs: normalizeText((row?.props?.map || []).map((item) => item?.value).join('-')),
          canBookCount: row?.props?.canBookCount ?? null,
          skuCount: Number(row?.props?.amount || 0),
          image: null,
          price: parseNumber(row?.props?.price),
          discountPrice: null,
        })).filter((item) => item.name || item.skuId);
        return sku.length ? { success: true, message: null, selectedSkuMap: {}, priceRanges: [], imageMap: {}, sku } : null;
      } catch { return null; }
    };
    const fromCartSider = (fiber) => {
      try {
        const children = fiber?.memoizedProps?.children || [];
        const submitOrderNode = children[children.length - 1]?.find?.((item) => item?.key === 'submitOrder');
        const props = submitOrderNode?.props?.data?.props;
        const mapperKey = Object.getOwnPropertySymbols(props?.dataManager || {}).find((key) => String(key).includes('mapper'));
        const mapper = mapperKey ? props?.dataManager?.[mapperKey] : null;
        const orderAmounts = props?.selectedOrder?.orderAmounts || {};
        const featureItems = (props?.dataManager?.params?.featureItems || []).flatMap((item) => item?.dataList || []).filter((item) => item?.imageUrl);
        let activePrice = null;
        const priceRanges = props?.dataManager?.params?.saleOptions?.priceRanges || [];
        if (props?.dataManager?.params?.saleOptions?.priceRangeMode && Array.isArray(priceRanges)) {
          const totalCount = Number(props?.selectedOrder?.totalCount || 0);
          priceRanges.forEach((range) => { if (totalCount >= Number(range?.beginAmount || 0)) activePrice = range?.price ?? activePrice; });
        }
        const sku = Object.keys(orderAmounts).map((key) => {
          const item = mapper?.[key] || {};
          const image = featureItems.find((entry) => normalizeText(item?.specAttrs || '').includes(normalizeText(entry?.label || '')))?.imageUrl || null;
          return {
            specId: item.specId || key,
            skuId: item.skuId || item.specId || key,
            name: normalizeText(item.specAttrs || key),
            specAttrs: normalizeText(item.specAttrs || key),
            canBookCount: item.canBookCount ?? null,
            skuCount: Number(orderAmounts[key] || 0),
            image: cleanImageUrl(image),
            price: parseNumber(activePrice || item.priceNum || item.price),
            discountPrice: null,
          };
        }).filter((item) => item.name || item.skuId);
        return sku.length ? { success: true, message: null, selectedSkuMap: orderAmounts, priceRanges, imageMap: {}, sku } : null;
      } catch { return null; }
    };
    for (const root of roots) {
      const fiber = findReactFiber(root);
      if (!fiber) continue;
      const result = fromPanel(fiber) || fromMoreDimension(fiber) || fromCartSider(fiber);
      if (result) return result;
    }
    return { success: false, message: 'runtime sku unavailable', selectedSkuMap: {}, priceRanges: [], imageMap: {}, sku: [] };
  };
  const images = dedupe([
    ...readImages([
      // 新版 1688 gallery 容器
      '#gallery img',
      '.gallery-img img', '.main-image img', '.detail-gallery-img img', '.thumb-img img', '.thumbnail img', '.detail-gallery img',
      '.preview-list img', '.fd-clr img', '.od-pc-offer-tab img', '.offer-detail-tab img', '.main-pic img', '.pic-view img',
      '.preview-wrap img', '.detail-pic img', '.product-image img', '.offer-image img', '.main-img img', '.img-list img',
      // 1688 新版详情页图库主图
      '.od-gallery-preview .preview-img', '.od-gallery-preview img',
      '.od-picture-gallery-list .v-image-cover',
      // 详情描述区产品图（在 description/mod-detail 区域内）
      '#offer-detail img', '.mod-detail img', '.detail-content img', '.detail-wrap img',
      '.description-content img', '.offer-content img', '.product-content img',
    ], document, 120),
    ...readResourceImages(120),
  ]).slice(0, 120);
  const productAttributePairs = dedupe([
    ...readAntDescriptionsPairs(document, 120),
    ...readPairsBySelectors(
      ['.module-od-product-attributes .ant-descriptions-row'],
      ['.ant-descriptions-item-label span'],
      ['.ant-descriptions-item-content .field-value', '.ant-descriptions-item-content']
    ),
  ]);
  const attributes = dedupe([
    ...productAttributePairs,
    ...readPairsBySelectors(['.offer-attr-item'], ['.offer-attr-item-name'], ['.offer-attr-item-value']),
    ...readPairsBySelectors(['#productAttributes .ant-descriptions-row', '.ant-descriptions-row'], ['.ant-descriptions-item-label span'], ['.ant-descriptions-item-content .field-value', '.ant-descriptions-item-content']),
    ...readPairsBySelectors(['.attr-item', '.product-attr-item'], ['.attr-name', '.attr-key'], ['.attr-value']),
    ...readPairsBySelectors(['.attr-table tr', '.product-params tr', 'table tr'], ['th', 'td:first-child'], ['td:last-child'])
  ]);
  // 收集所有 SKU 图片：从 DOM 按钮 + performance resources 双向覆盖
  const skuImageMap = {};
  queryAll(['.sku-filter-button img', '.sku-filter-button .ant-image-img']).forEach((img) => {
    const label = normalizeText(img.closest('.sku-filter-button')?.querySelector('.label-name')?.innerText || img.alt || '');
    const src = cleanImageUrl(img.src || img.getAttribute('src'));
    if (label && src) skuImageMap[label] = src;
  });

  const optionGroups = [];
  /* Scan SKU containers: .feature-item and .transverse-filter */
const skuContainers = [
  ...queryAll(['#skuSelection .feature-item']),
  ...queryAll(['.transverse-filter']),
];
skuContainers.forEach((featureEl, featureIndex) => {
    const groupName = pickTextFrom(featureEl, ['.feature-item-label h3', '.feature-item-label', 'h3']) || `规格组${featureIndex + 1}`;
    const values = [];
    queryAll(['.sku-filter-button', '.prop-item-inner-wrapper', '.selector-prop-item', '.expand-view-item'], featureEl).forEach((el) => {
      const name = pickTextFrom(el, ['.label-name', '.prop-name', '.prop-item-text', 'span']) || normalizeText(el.getAttribute?.('title'));
      let image = cleanImageUrl(pickAttrFrom(el, ['img'], 'src'));
      if (!image) {
        const bgEl = el.querySelector('.prop-img, .sku-item-image, .single-sku-img-pop');
        const bg = bgEl ? window.getComputedStyle(bgEl).backgroundImage : '';
        const match = bg && bg.match(/https?:[^)"]+/i);
        if (match) image = cleanImageUrl(match[0]);
      }
      if (name) {
        if (!image) image = skuImageMap[name] || null;
        values.push({ name, image: image || null });
      }
    });
    queryAll(['.expand-view-item'], featureEl).forEach((el) => {
      const name = pickTextFrom(el, ['.item-label', 'span']) || null;
      const price = pickTextFrom(el, ['.item-price-stock', '.price']) || null;
      if (name) values.push({ name, image: null, price: parseNumber(price) });
    });
    const dedupedValues = dedupe(values);
    if (dedupedValues.length) optionGroups.push({ name: groupName, values: dedupedValues });
  });

  const packagingRows = [];
  const packagingTable = document.getElementById('productPackInfo')?.querySelector('table') || document.querySelector('.module-od-product-pack-info table');
  const packagingHeaders = Array.from(packagingTable?.querySelectorAll?.('thead th') || []).map((cell) => normalizeText(cell.innerText || cell.textContent));
  const packagingTableText = normalizeText(packagingTable?.innerText || packagingTable?.textContent);
  const packagingWeightIndex = packagingHeaders.findIndex((header) => /重量|毛重|净重|weight/i.test(header || ''));
  const packagingLengthIndex = packagingHeaders.findIndex((header) => /(^|[^总])长\s*\(?\s*(cm|mm)?\s*\)?$|长度|length/i.test(header || ''));
  const packagingWidthIndex = packagingHeaders.findIndex((header) => /宽\s*\(?\s*(cm|mm)?\s*\)?$|宽度|width/i.test(header || ''));
  const packagingHeightIndex = packagingHeaders.findIndex((header) => /高\s*\(?\s*(cm|mm)?\s*\)?$|高度|height/i.test(header || ''));
  const packagingSpecIndex = packagingHeaders.findIndex((header) => /规格|尺码|spec|size/i.test(header || ''));
  const packagingColorIndex = packagingHeaders.findIndex((header) => /颜色|色系|color/i.test(header || ''));
  const packTbodyRows = (document.getElementById('productPackInfo') || document.querySelector('.module-od-product-pack-info'))?.querySelectorAll?.('table tbody tr') || [];
  Array.from(packTbodyRows).forEach((row) => {
    const cells = Array.from(row.querySelectorAll('td')).map((cell) => normalizeText(cell.innerText || cell.textContent));
    if (cells.length >= 1) {
      const rowData = {
        color: packagingColorIndex >= 0 && packagingColorIndex < cells.length ? cells[packagingColorIndex] : (cells.length >= 1 ? cells[0] : null),
        capacity: packagingSpecIndex >= 0 && packagingSpecIndex < cells.length ? cells[packagingSpecIndex] : (cells.length >= 2 ? cells[1] : null),
        weightText: packagingWeightIndex >= 0 && packagingWeightIndex < cells.length ? cells[packagingWeightIndex] : null,
        weightGrams: packagingWeightIndex >= 0 && packagingWeightIndex < cells.length ? parseInteger(cells[packagingWeightIndex]) : null,
        lengthText: packagingLengthIndex >= 0 && packagingLengthIndex < cells.length ? cells[packagingLengthIndex] : null,
        widthText: packagingWidthIndex >= 0 && packagingWidthIndex < cells.length ? cells[packagingWidthIndex] : null,
        heightText: packagingHeightIndex >= 0 && packagingHeightIndex < cells.length ? cells[packagingHeightIndex] : null,
      };
      if (cells.length === 1 && packagingWeightIndex === 0) {
        rowData.color = null;
        rowData.capacity = null;
        rowData.weightText = cells[0];
        rowData.weightGrams = parseInteger(cells[0]);
      }
      packagingRows.push(rowData);
    }
  });

  /* ── Fallback: extract dimensions from #detail description text ──
     When productPackInfo table is missing or has no L/W/H columns,
     scan the product detail/description section for patterns like:
       MEAS:51*35*42CM   尺寸：30*9.5*4.5cm   单个：34*25*2CM
       箱规：45*40*30cm   净重：53g   含包装：61.8g
  */
  if (packagingRows.length === 0 || packagingRows.every(r => !r.lengthText && !r.widthText && !r.heightText)) {
    // 尝试多个来源提取尺寸信息
    const sources = [
      document.querySelector('#detail'),
      document.querySelector('#offer-template-0'),
      document.querySelector('.mod-detail'),
      document.querySelector('.description-content'),
      document.querySelector('[data-module=\"od_product_detail\"]'),
    ].filter(Boolean);
    let detailText = sources.map(el => (el.textContent || el.innerText || '')).join(' ');
    detailText = detailText.replace(/\\s+/g, ' ').trim();
    // 如果 detail 区域没有内容，用整个 body（限制到含尺寸关键词的行）
    if (!detailText || detailText.length < 20) {
      const bodyText = (document.body.textContent || document.body.innerText || '');
      const lines = bodyText.split(/[\\n\\r]+/).filter(l =>
        /MEAS|尺寸|单个|箱规|净重|重量|包装.*重|specification|dimension/i.test(l)
      );
      detailText = lines.join(' ');
    }
    // 提取尺寸: L*W*H (cm or CM or mm)
    const dimPatterns = [
      /(?:尺寸|单个|产品尺寸|规格|MEAS|meas)[：:\s]*(\d+\.?\d*)\s*\*\s*(\d+\.?\d*)\s*\*\s*(\d+\.?\d*)\s*(?:cm|CM|mm)?/i,
      /(?:长|L)[：:\s]*(\d+\.?\d*)\s*(?:cm|CM|mm)?[,\s，]+\s*(?:宽|W)[：:\s]*(\d+\.?\d*)\s*(?:cm|CM|mm)?[,\s，]+\s*(?:高|H)[：:\s]*(\d+\.?\d*)\s*(?:cm|CM|mm)?/i,
      /(\d+\.?\d*)\s*\*\s*(\d+\.?\d*)\s*\*\s*(\d+\.?\d*)\s*(?:cm|CM|mm)/i,
    ];
    let fallbackDims = null;
    for (const pat of dimPatterns) {
      const m = detailText.match(pat);
      if (m) {
        const l = parseFloat(m[1]), w = parseFloat(m[2]), h = parseFloat(m[3]);
        if (l > 0 && w > 0 && h > 0 && l < 300 && w < 300 && h < 300) {  // sanity: < 3m
          fallbackDims = { lengthCm: l, widthCm: w, heightCm: h };
          break;
        }
      }
    }
    // 提取重量: 净重/含包装/重量
    const weightPatterns = [
      /(?:净重|重量|含包装|毛重|产品重量)[：:\s]*(\d+\.?\d*)\s*(?:g|G|克)/i,
      /(?:weight|Weight)[：:\s]*(\d+\.?\d*)\s*(?:g|G)/i,
    ];
    let fallbackWeight = null;
    for (const pat of weightPatterns) {
      const m = detailText.match(pat);
      if (m) {
        const w = parseFloat(m[1]);
        if (w > 0 && w < 100000) {
          fallbackWeight = w;
          break;
        }
      }
    }
    // 合并到 packagingRows
    if (fallbackDims || fallbackWeight) {
      const existing = packagingRows.length > 0 ? packagingRows[0] : {};
      if (fallbackDims) {
        existing.lengthText = String(fallbackDims.lengthCm);
        existing.widthText = String(fallbackDims.widthCm);
        existing.heightText = String(fallbackDims.heightCm);
      }
      if (fallbackWeight && !existing.weightGrams) {
        existing.weightGrams = fallbackWeight;
        existing.weightText = String(fallbackWeight);
      }
      if (packagingRows.length === 0) {
        packagingRows.push(existing);
      } else {
        packagingRows[0] = existing;
      }
    }
  }

  const skuDetails = [];
  const addSku = (name, price, image) => {
    const cleanName = normalizeText(name);
    if (!cleanName) return;
    skuDetails.push({ name: cleanName, price: parseNumber(price), image: cleanImageUrl(image) });
  };
  queryAll(['.sku-item-wrapper']).forEach((el, index) => {
    addSku(pickTextFrom(el, ['.sku-item-name', '.sku-item-name-text', 'span']) || `规格${index + 1}`, pickTextFrom(el, ['.discountPrice-price', '.sku-item-price', '.price']), pickAttrFrom(el, ['.sku-item-img img', '.sku-wrapper-img img', 'img'], 'src'));
  });
  queryAll(['.expand-view-item', '.sku-list-item', '.single-sku-list-wrap']).forEach((el, index) => {
    addSku(pickTextFrom(el, ['.item-label', '.sku-item-name-text', '.single-sku-title span:nth-child(2)', 'span']) || `规格${index + 1}`, pickTextFrom(el, ['.item-price-stock', '.sku-item-price', '.single-price-warp .price-title', '.price']), pickAttrFrom(el, ['img'], 'src'));
  });
  queryAll(['.next-table-body table tr']).forEach((el, index) => {
    addSku(pickTextFrom(el, ['span.normal-text', 'td span', 'td']) || `规格${index + 1}`, pickTextFrom(el, ['.price', 'td:last-child', 'td']), null);
  });
  const runtimeSkuData = extractRuntimeSkuData();
  const initData = getWindowInitData();
  const pageStructuredData = collectPageStructuredData();
  const titleModule = document.getElementById('module-od-title');
  const titleContent = titleModule?.querySelector('.title-content') || document.querySelector('.title-content');
  const title = (titleContent ? normalizeText(titleContent.innerText || titleContent.textContent) : null) || pickText(['.title-text', 'h1', '.d-title']) || normalizeText(document.title);
  const priceComp = document.querySelector('.price-comp');
  const price = (priceComp ? normalizeText(priceComp.innerText || priceComp.textContent) : null)
    || pickText(['.price-info', '.price-now', '.discountPrice-price', '.price', '.current-price', '.item-price-stock', '.ma-ref-price'])
    || (runtimeSkuData.sku?.[0]?.price != null ? String(runtimeSkuData.sku[0].price) : null);
  const seller = pickText(['.shop-company-name', 'a[href*="company"]', '[class*="company"] a', '[class*="shop"] a', '.header-shop-name', '.shop-name']);
  const sellerLink = cleanUrl(pickAttr(['a[href*="company"]', '[class*="company"] a', '[class*="shop"] a'], 'href'));
  const minOrderQtyText = pickText(['.moq-number', '.quantity-range', '.lt-spec-num', '[class*="moq"]']) || attributes.find((item) => /起订|最小|采购量|minimum/i.test(item.name || ''))?.value || null;
  const brand = pickText(['a[href*="brand"]', '[class*="brand"]']) || attributes.find((item) => /品牌/i.test(item.name || ''))?.value || null;
  const origin = attributes.find((item) => /产地|origin/i.test(item.name || ''))?.value || null;
  const model = attributes.find((item) => /型号|model/i.test(item.name || ''))?.value || null;
  const description = readDescriptionBlock([
    '#detail', '#offer-template-0', '#detailContentContainer',
    '.html-description', '.offer-detail-tab', '.mod-detail',
    '.description-content', '.detail-content',
  ]);
  const bodyText = (document.body?.innerText || document.body?.textContent || '').slice(0, 3000);
  const hasStrongProductSignals = Boolean(
    title && images.length > 0 && (
      attributes.length >= 3 ||
      optionGroups.length > 0 ||
      packagingRows.length > 0 ||
      skuDetails.length > 0 ||
      (runtimeSkuData?.sku?.length || 0) > 0
    )
  );
  /* ── Shipping / delivery info ── */
  const shipping = {};
  const shippingEl = document.querySelector('.cart-content') || document.querySelector('.module-od-shipping-services, #shippingServices');
  if (shippingEl) {
    const shippingText = normalizeText(shippingEl.innerText || shippingEl.textContent || '');
    // 提取 .cart-content 中的具体字段（比整体 innerText 更精确）
    const cartContent = shippingEl.querySelector('.cart-content');
    const cartText = cartContent ? normalizeText(cartContent.innerText || cartContent.textContent || '') : shippingText;
    // 发货地: .location span
    const locEl = shippingEl.querySelector('.location');
    if (locEl) shipping.origin = normalizeText(locEl.innerText || locEl.textContent || '');
    // 运费: "运费¥5起" or "运费 ¥5"
    const freightMatch = cartText.match(/运费\s*[¥￥]\s*(\d+(?:\.\d+)?)/);
    if (freightMatch) shipping.freightCny = parseFloat(freightMatch[1]);
    // 快递公司
    const carrierMatch = cartText.match(/(?:常发|快递[：:]?\s*)([一-龥a-zA-Z]{2,10}(?:快递|物流|速运|Express)?)/);
    if (carrierMatch) shipping.carrier = carrierMatch[1];
    // 发货时效
    const dispatchMatch = cartText.match(/预计(\d+)小时发货/);
    if (dispatchMatch) shipping.dispatchHours = parseInt(dispatchMatch[1]);
    // 送达时效
    if (/后天送达/.test(cartText)) shipping.deliveryDays = 2;
    else if (/明天送达/.test(cartText)) shipping.deliveryDays = 1;
    else { const dm = cartText.match(/(\d+)天送达/); if (dm) shipping.deliveryDays = parseInt(dm[1]); }
    // 起批量
    const moqEl = shippingEl.querySelector('.moq-number, [class*=\"moq\"]');
    if (moqEl) {
      const moqText = normalizeText(moqEl.innerText || moqEl.textContent || '');
      const moqMatch = moqText.match(/(\d+)/);
      if (moqMatch) shipping.minOrderQty = parseInt(moqMatch[1]);
    }
    // 兜底：如果以上都没匹配到，保存原始文本用于调试
    if (!shipping.origin && !shipping.freightCny && !shipping.carrier) {
      shipping._debugText = shippingText.substring(0, 200);
    }
  }
  /* ── Shop stats (回头率, 服务分, 好评率) ── */
  const shopDataEl = document.querySelector('.shop-data');
  const shopStats = shopDataEl ? normalizeText(shopDataEl.innerText || shopDataEl.textContent || '') : null;
  const loginRequired = !hasStrongProductSignals && (
    /passport|login|member\.1688\.com/i.test(location.href) ||
    /扫码登录|密码登录|短信登录/i.test(bodyText) ||
    (/登录|login/i.test(bodyText) && !title)
  );
  const captchaDetected = !!(
    document.querySelector('.nc-container') ||  // 滑块验证
    document.querySelector('#nc_1_wrapper') ||  // 滑块容器
    document.querySelector('.slide-verify') ||  // 滑块
    document.querySelector('.captcha') ||       // 通用验证码
    document.querySelector('#J_Checkcode') ||   // 1688 验证码
    (document.querySelector('.J_MIDDLEWARE_FRAMEWRAP') && document.title.includes('验证'))
  );
  return {
    site: '1688',
    url: location.href,
    loginRequired,
    captchaDetected,
    title,
    price,
    priceValue: parseNumber(price),
    brand,
    seller,
    sellerLink,
    image: images[0] || null,
    images,
    video: cleanUrl(pickAttr(['video source', '.video-player source', 'video'], 'src')),
    minOrderQty: minOrderQtyText,
    minOrderQtyValue: parseInteger(minOrderQtyText),
    origin,
    model,
    attributes,
    optionGroups,
    packagingHeaders,
    packagingTableText,
    packagingRows: dedupe(packagingRows),
    shipping,
    shopStats,
    skuDetails: dedupe(skuDetails),
    runtimeSkuData,
    pageStructuredData,
    description,
    sourceHints: {
      productAttributeCount: productAttributePairs.length,
      attributeCount: attributes.length,
      optionGroupCount: optionGroups.length,
      skuCount: skuDetails.length,
      imageCount: images.length,
      runtimeSkuCount: runtimeSkuData?.sku?.length || 0,
      runtimeImageMappingCount: Object.keys(runtimeSkuData?.imageMap || {}).length,
      initDataKeys: Object.keys((initData && typeof initData === 'object') ? initData : {}).slice(0, 20),
      pageStructuredDataCount: pageStructuredData.length,
      packagingHeaderCount: packagingHeaders.length,
      packagingTableTextLength: packagingTableText ? packagingTableText.length : 0,
    },
  };
})()
"""


def _profile_dir(profile: str) -> Path:
    path = DATA_DIR / 'browser' / 'profiles' / '1688' / profile
    path.mkdir(parents=True, exist_ok=True)
    return path


def _candidate_browser_paths() -> list[str]:
    import platform
    system = platform.system()

    # PATH-named executables (Chromium-based browsers, cross-platform)
    paths = [
        'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser',
        'chrome', 'msedge', 'microsoft-edge',
        'brave', 'brave-browser',           # Brave
        'opera', 'vivaldi',                  # Opera / Vivaldi
        '360chrome', '360se',                # 360 浏览器
        'qqbrowser',                         # QQ 浏览器
        'sogou-explorer', 'sogou',           # 搜狗浏览器
        'liebao',                            # 猎豹浏览器
        'maxthon',                           # 傲游浏览器
        'doubao', 'doubao-browser',          # 豆包浏览器 (抖音)
    ]

    if system == 'Darwin':
        paths.extend([
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            '/Applications/Chromium.app/Contents/MacOS/Chromium',
            '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
            '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
            '/Applications/Opera.app/Contents/MacOS/Opera',
            '/Applications/Vivaldi.app/Contents/MacOS/Vivaldi',
        ])
    elif system == 'Windows':
        import os as _os
        for base in [
            _os.environ.get('ProgramFiles', 'C:\\Program Files'),
            _os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)'),
            _os.environ.get('LOCALAPPDATA', ''),
        ]:
            if base:
                paths.extend([
                    # Chrome
                    f'{base}\\Google\\Chrome\\Application\\chrome.exe',
                    # Edge (pre-installed on Windows 10+, highest priority for Windows users)
                    f'{base}\\Microsoft\\Edge\\Application\\msedge.exe',
                    # Brave
                    f'{base}\\BraveSoftware\\Brave-Browser\\Application\\brave.exe',
                    # Opera
                    f'{base}\\Opera\\opera.exe',
                    # 360 浏览器 (China)
                    f'{base}\\360Chrome\\Chrome\\Application\\360chrome.exe',
                    f'{base}\\360\\360se6\\Application\\360se.exe',
                ])

    # Playwright-bundled Chromium (fallback when no system Chrome) — all platforms
    paths.extend(_playwright_chromium_paths())
    return paths


def _playwright_chromium_paths() -> list[str]:
    """Find Playwright's bundled Chromium — fallback when no system Chrome."""
    import os
    import platform
    home = Path.home()
    if platform.system() == 'Darwin':
        cache_dir = home / 'Library' / 'Caches' / 'ms-playwright'
        suffix = 'chrome-mac/Chromium.app/Contents/MacOS/Chromium'
    elif platform.system() == 'Linux':
        cache_dir = home / '.cache' / 'ms-playwright'
        suffix = 'chrome-linux/chrome'
    elif platform.system() == 'Windows':
        cache_dir = Path(os.environ.get('LOCALAPPDATA', home / 'AppData' / 'Local')) / 'ms-playwright'
        suffix = 'chrome-win/chrome.exe'
    else:
        return []

    paths: list[str] = []
    if cache_dir.is_dir():
        for entry in sorted(cache_dir.iterdir(), reverse=True):
            if entry.name.startswith('chromium-'):
                candidate = entry / suffix
                if candidate.exists():
                    paths.append(str(candidate))
    return paths


def find_browser_executable(explicit: str | None = None) -> str | None:
    candidate = str(explicit or '').strip()
    if candidate:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        found = shutil.which(candidate)
        if found:
            return found
        raise ConfigError(f'浏览器可执行文件不存在: {candidate}')

    # Phase 1: check all known paths (fast, no subprocess)
    for item in _candidate_browser_paths():
        found = shutil.which(item)
        if found:
            return found
        path = Path(item)
        if path.exists():
            return str(path)

    # Phase 2: platform-specific deep search (slower, only runs if Phase 1 fails)
    import platform
    system = platform.system()

    if system == 'Darwin':
        # macOS: use Spotlight (mdfind) to find Chromium-based browsers
        browser_apps = [
            'Google Chrome', 'Microsoft Edge', 'Brave Browser',
            'Chromium', 'Opera', 'Vivaldi',
        ]
        for app_name in browser_apps:
            try:
                result = subprocess.run(
                    ['mdfind', f'kMDItemKind == "Application" && kMDItemDisplayName == "{app_name}"'],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.strip().split('\n'):
                    line = line.strip()
                    if line.endswith('.app'):
                        executable = f'{line}/Contents/MacOS/{app_name}'
                        if Path(executable).exists():
                            return executable
            except Exception:
                pass

    elif system == 'Windows':
        # Windows: try registry query for default browser, then common install paths
        import os as _os
        # Check Chrome via registry
        for reg_key in [
            r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
            r'HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
        ]:
            try:
                result = subprocess.run(
                    ['reg', 'query', reg_key, '/ve'],
                    capture_output=True, text=True, timeout=5,
                )
                for line in result.stdout.split('\n'):
                    if '.exe' in line.lower():
                        path = line.strip().rsplit('    ', 1)[-1].strip()
                        if Path(path).exists():
                            return path
            except Exception:
                pass
        # Edge via registry
        try:
            result = subprocess.run(
                ['reg', 'query', r'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe', '/ve'],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.split('\n'):
                if '.exe' in line.lower():
                    path = line.strip().rsplit('    ', 1)[-1].strip()
                    if Path(path).exists():
                        return path
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════

    # Phase 2.5: Detect running Chromium browser — prefer what user is already using
    # This catches cases where Edge or Brave is running but Chrome is first in the path list
    try:
        _commands = _list_browser_commands()
        seen = set()
        if platform.system() == 'Windows':
            browser_exes_win = ['chrome.exe', 'msedge.exe', 'brave.exe', 'opera.exe']
            for line in _commands:
                lower_line = line.lower()
                if any(h in lower_line for h in ['helper', 'renderer', 'gpu-process']):
                    continue
                for exe in browser_exes_win:
                    if exe in lower_line:
                        # Extract executable path from command line
                        parts = line.split()
                        for part in parts:
                            if exe in part.lower():
                                p = part.strip('"')
                                if Path(p).exists() and p not in seen:
                                    seen.add(p)
                                break
                        break
        else:
            browser_exes = [
                '/Google Chrome.app/Contents/MacOS/Google Chrome',
                '/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
                '/Brave Browser.app/Contents/MacOS/Brave Browser',
                '/Chromium.app/Contents/MacOS/Chromium',
                '/Opera.app/Contents/MacOS/Opera',
                '/Vivaldi.app/Contents/MacOS/Vivaldi',
            ]
            for line in _commands:
                if any(h in line for h in ['Helper', 'helper', 'renderer', 'gpu-process']):
                    continue
                for exe in browser_exes:
                    idx = line.find(exe)
                    if idx >= 0:
                        p = line[idx:idx + len(exe)]
                        if Path(p).exists() and p not in seen:
                            seen.add(p)
                        break
    except Exception:
        pass

    # Phase 3: Playwright bundled Chromium (pip install playwright)
    # ═══════════════════════════════════════════════════════════════════════
    try:
        import platform as _plat
        import playwright  # noqa: F401
        import glob as _glob

        system = _plat.system()
        if system == 'Darwin':
            pattern = str(Path.home() / 'Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium')
        elif system == 'Linux':
            pattern = str(Path.home() / '.cache/ms-playwright/chromium-*/chrome-linux/chrome')
        elif system == 'Windows':
            pattern = str(Path.home() / 'AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe')
        else:
            pattern = ''

        if pattern:
            matches = sorted(_glob.glob(pattern), reverse=True)
            if matches and Path(matches[0]).exists():
                return matches[0]
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 4: Auto-install Playwright Chromium (last resort, slow but guaranteed)
    # ═══════════════════════════════════════════════════════════════════════
    if _auto_install_browser():
        # Recurse once to find the newly installed browser
        try:
            import platform as _plat2
            import glob as _glob2
            system = _plat2.system()
            if system == 'Darwin':
                pattern = str(Path.home() / 'Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium')
            elif system == 'Linux':
                pattern = str(Path.home() / '.cache/ms-playwright/chromium-*/chrome-linux/chrome')
            elif system == 'Windows':
                pattern = str(Path.home() / 'AppData/Local/ms-playwright/chromium-*/chrome-win/chrome.exe')
            else:
                pattern = ''
            if pattern:
                matches = sorted(_glob2.glob(pattern), reverse=True)
                if matches and Path(matches[0]).exists():
                    return matches[0]
        except Exception:
            pass
        # Fallback: re-run Phase 1 now that browser is installed
        for item in _candidate_browser_paths():
            found = shutil.which(item)
            if found:
                return found
            path = Path(item)
            if path.exists():
                return str(path)

    return None


def _build_summary(probe: dict[str, Any]) -> dict[str, Any]:
    return {
        'title': probe.get('title'),
        'price': probe.get('price'),
        'brand': probe.get('brand'),
        'seller': probe.get('seller'),
        'image_count': len(probe.get('images') or []),
        'attribute_count': len(probe.get('attributes') or []),
        'dom_sku_count': len(probe.get('skuDetails') or []),
        'runtime_sku_count': len((probe.get('runtimeSkuData') or {}).get('sku') or []),
        'login_required': bool(probe.get('loginRequired')),
        'captcha_detected': bool(probe.get('captchaDetected')),
    }


def _has_strong_product_signals(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict):
        return False
    return bool(
        probe.get('title')
        and len(probe.get('images') or []) > 0
        and (
            len(probe.get('attributes') or []) >= 3
            or len(probe.get('optionGroups') or []) > 0
            or len(probe.get('packagingRows') or []) > 0
            or len(probe.get('skuDetails') or []) > 0
            or len((probe.get('runtimeSkuData') or {}).get('sku') or []) > 0
        )
    )


def _looks_like_failure_page(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict):
        return True
    url = str(probe.get('url') or '').lower()
    title = str(probe.get('title') or '').lower()
    description = str(probe.get('description') or '').lower()
    body_hints = ' '.join([
        title,
        description,
        str((probe.get('seller') or '')).lower(),
        str((probe.get('error') or '')).lower(),
    ])
    failure_tokens = [
        'wrongpage',
        'notfound',
        'page.1688.com/shtml/static/wrongpage',
        'spm=a260k.24848612.notfound',
        '404',
        '页面不存在',
        '商品不存在',
        '不存在或已下架',
    ]
    if any(token in url or token in body_hints for token in failure_tokens):
        return True
    if 'login.taobao.com' in url or 'login.1688.com' in url:
        return True
    return False


def _looks_like_captcha_intercept(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict):
        return False
    url = str(probe.get('url') or '').lower()
    title = str(probe.get('title') or '').lower()
    description = str(probe.get('description') or '').lower()
    seller = str(probe.get('seller') or '').lower()
    body_hints = ' '.join([title, description, seller, str(probe.get('error') or '').lower()])
    captcha_tokens = [
        '验证码拦截',
        '验证码',
        'captcha',
        'security check',
        '人机验证',
        '访问验证',
        '请完成验证',
    ]
    return any(token in url or token.lower() in body_hints for token in captcha_tokens)


def _effective_poll_timeout_seconds(timeout_seconds: int, *, headed: bool, login_required: bool) -> int:
    base = max(int(timeout_seconds), 1)
    if headed and login_required:
        return max(base, 180)
    return base


def _probe_ready(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict):
        return False
    if probe.get('loginRequired') and not _has_strong_product_signals(probe):
        return False
    if _looks_like_captcha_intercept(probe):
        return False
    if _looks_like_failure_page(probe):
        return False
    if len(((probe.get('runtimeSkuData') or {}).get('sku') or [])) > 0:
        return True
    if len(probe.get('skuDetails') or []) > 0:
        return True
    if _has_strong_product_signals(probe):
        return True
    if len(probe.get('attributes') or []) >= 3:
        return True
    return False


def _single_pass_probe(tab: CdpTab) -> dict[str, Any]:
    try:
        # Scroll to trigger lazy DOM, then extract
        tab.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});")
        time.sleep(0.4)
        tab.evaluate("window.scrollTo({top: 0, behavior: 'instant'});")
        time.sleep(0.2)
        current = tab.evaluate(EXTRACT_1688_JS)
    except Exception as exc:
        current = {'url': getattr(tab, 'url', None), 'error': str(exc), 'loginRequired': False}
    current['attempt'] = 1
    current['elapsed_seconds'] = 0.0
    current['ready'] = bool(_probe_ready(current))
    current['single_pass'] = True
    return current


def _poll_probe(tab: CdpTab, timeout_seconds: int, poll_ms: int, *, headed: bool = False) -> dict[str, Any]:
    started = time.time()
    attempt = 0
    last: dict[str, Any] = {}
    effective_timeout = max(int(timeout_seconds), 1)
    while time.time() - started < effective_timeout:
        attempt += 1
        try:
            tab.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});")
            time.sleep(0.3)
            current = tab.evaluate(EXTRACT_1688_JS)
        except Exception as exc:
            # 检测致命断连（浏览器崩溃/关闭），立即退出
            err_lower = str(exc).lower()
            if any(kw in err_lower for kw in ('target closed', 'browser closed', 'connection refused', 'disconnected', 'browser has been closed')):
                return {'ready': False, 'error': f'CDP disconnected: {exc}', 'fatal': True,
                        'elapsed_seconds': round(time.time() - started, 2)}
            current = {'url': getattr(tab, 'url', None), 'error': str(exc), 'loginRequired': False}
        current['attempt'] = attempt
        current['elapsed_seconds'] = round(time.time() - started, 2)
        login_required = bool(current.get('loginRequired'))
        effective_timeout = max(effective_timeout, _effective_poll_timeout_seconds(timeout_seconds, headed=headed, login_required=login_required))
        if headed and login_required:
            current['interactive_login_wait'] = True
            current['effective_timeout_seconds'] = effective_timeout
        last = current
        if _probe_ready(current):
            current['ready'] = True
            return current
        # Captcha detected — pause and wait for user to solve it
        if current.get('captchaDetected') or _looks_like_captcha_intercept(current):
            print('\n⚠️  检测到 1688 验证码，请在浏览器中手动滑动验证', file=sys.stderr)
            print('   验证完成后按 Enter 继续...', file=sys.stderr)
            try:
                input()
            except EOFError:
                pass
            # Give the page a moment to update after captcha solve
            time.sleep(2)
            continue  # Re-probe immediately instead of sleeping
        time.sleep(poll_ms / 1000)
    last['ready'] = False
    last['timed_out'] = True
    last['elapsed_seconds'] = round(time.time() - started, 2)
    last['effective_timeout_seconds'] = effective_timeout
    return last




def _login_url() -> str:
    return 'https://login.1688.com/member/signin.htm'


def _maybe_open_login_first(tab: CdpTab, *, headed: bool, timeout_ms: int) -> None:
    if not headed:
        return
    try:
        tab.navigate(_login_url(), wait_until='domcontentloaded', timeout=max(timeout_ms // 1000, 1))
        try:
            tab.wait_for_load(timeout=5)
        except Exception:
            pass
    except Exception:
        return



def _session_file(profile: str) -> Path:
    path = DATA_DIR / 'browser' / 'sessions'
    path.mkdir(parents=True, exist_ok=True)
    return path / f'1688-{profile}.json'


def _extract_offer_id(url: str | None) -> str | None:
    value = str(url or '').strip()
    match = re.search(r'/offer/(\d+)', value)
    return match.group(1) if match else None


def _page_matches_target_offer(page: Any, target_url: str) -> bool:
    page_url = str(getattr(page, 'url', '') or '')
    if '1688.com' not in page_url:
        return False
    target_offer_id = _extract_offer_id(target_url)
    page_offer_id = _extract_offer_id(page_url)
    if target_offer_id and page_offer_id:
        return target_offer_id == page_offer_id
    return page_url == target_url




def _load_browser_session(profile: str) -> dict[str, Any] | None:
    target = _session_file(profile)
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding='utf-8'))
    except Exception:
        return None


def _write_browser_session(profile: str, payload: dict[str, Any]) -> Path:
    import tempfile
    import os as _os_atomic
    target = _session_file(profile)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix='.session-', suffix='.json')
    try:
        _os_atomic.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
        _os_atomic.fsync(fd)
    finally:
        _os_atomic.close(fd)
    # Windows: os.replace 可能因文件锁失败，重试 3 次
    for _retry in range(3):
        try:
            _os_atomic.replace(tmp, str(target))
            break
        except PermissionError:
            time.sleep(0.2)
    else:
        _os_atomic.replace(tmp, str(target))  # 最后一次不捕获
    return target


def _list_browser_commands() -> list[str]:
    """跨平台获取浏览器进程命令行（替代 ps -axo）。"""
    import platform as _plat
    try:
        if _plat.system() == 'Windows':
            r = subprocess.run(
                ['wmic', 'process', 'where', "name='chrome.exe' or name='msedge.exe'",
                 'get', 'CommandLine', '/format:list'],
                capture_output=True, text=True, timeout=5,
            )
            return [
                l.split('=', 1)[1].strip()
                for l in r.stdout.splitlines()
                if l.startswith('CommandLine=') and l.strip()
            ]
        else:
            r = subprocess.run(
                ['ps', '-axo', 'command='],
                capture_output=True, text=True, timeout=5,
            )
            return [l for l in (r.stdout or '').splitlines() if l.strip()]
    except Exception:
        return []


def _cdp_available(cdp_url: str) -> bool:
    try:
        with urlopen(cdp_url + '/json/version', timeout=5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode('utf-8'))
            if 'Browser' not in data:
                return False
            # Exclude Electron apps (e.g. VS Code, POUNDING-Dev) that expose CDP
            # but aren't real Chrome and can't navigate to 1688 pages
            ua = str(data.get('User-Agent', ''))
            if 'Electron' in ua:
                return False
            return True
    except Exception:
        return False


def _chrome_user_data_dir_matches(cdp_url: str, expected_dir: str) -> bool:
    """Verify that the Chrome at cdp_url uses the expected --user-data-dir profile.

    Returns True if the Chrome's profile matches expected_dir, or if we can't
    determine the profile (conservative: don't break existing sessions).
    """
    if not expected_dir:
        return True  # No expectation → don't block
    expected_resolved = str(Path(expected_dir).resolve())
    commands = _list_browser_commands()
    if not commands:
        return True  # Can't check → don't block (conservative)

    port_match = re.search(r':(\d+)/?', cdp_url)
    if not port_match:
        return True
    port = port_match.group(1)

    for line in commands:
        if f'--remote-debugging-port={port}' not in line:
            continue
        dir_match = re.search(r'--user-data-dir=(\S+)', line)
        if dir_match:
            actual = str(Path(dir_match.group(1)).resolve())
            return actual == expected_resolved
        # Chrome launched without explicit --user-data-dir → can't verify
        return True
    # CDP is alive but no Chrome process with --remote-debugging-port found
    # → likely Electron or another app, NOT our Chrome
    return False


def _find_live_cdp_session_for_profile(
    profile: str,
    session: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    current = dict(session or {})
    expected_user_data_dir = str(current.get('user_data_dir') or _profile_dir(profile)).strip()

    commands = _list_browser_commands()
    if not commands:
        return None

    exact_matches: list[dict[str, Any]] = []
    loose_matches: list[dict[str, Any]] = []

    for command in commands:
        if '--remote-debugging-port=' not in command:
            continue
        port_match = re.search(r'--remote-debugging-port=(\d+)', command)
        if not port_match:
            continue
        port = int(port_match.group(1))
        cdp_url = f'http://127.0.0.1:{port}'
        if not _cdp_available(cdp_url):
            continue

        matched_user_data_dir = expected_user_data_dir
        data_dir_match = re.search(r'--user-data-dir=(\S+)', command)
        actual_data_dir = data_dir_match.group(1) if data_dir_match else ''

        entry = {
            **current,
            'profile': profile,
            'user_data_dir': actual_data_dir or expected_user_data_dir,
            'remote_debugging_port': port,
            'cdp_url': cdp_url,
        }

        if expected_user_data_dir and f'--user-data-dir={expected_user_data_dir}' in command:
            exact_matches.append(entry)
        else:
            # AutoConnect fallback: any Chrome with CDP enabled
            loose_matches.append(entry)

    # Prefer exact profile match, fall back to any live CDP session
    # Only SAVE exact matches — loose matches belong to other repos/profiles
    resolved = None
    should_save = False
    if exact_matches:
        resolved = exact_matches[-1]
        should_save = True
    elif loose_matches:
        resolved = loose_matches[-1]
        should_save = False  # Don't persist another repo's Chrome session
    if resolved is None:
        return None
    resolved.setdefault('created_at', int(time.time()))
    if should_save:
        try:
            _write_browser_session(profile, resolved)
        except Exception:
            pass
    return resolved


def _resolve_browser_session(profile: str) -> dict[str, Any]:
    import platform
    session = _load_browser_session(profile) or {}
    # Expire login after 24h
    if session.get('login_detected'):
        created = session.get('created_at', 0)
        if created and time.time() - created > 86400:
            session.pop('login_detected', None)
    cdp_url = str(session.get('cdp_url') or '').strip()
    # 如果 CDP 可用，直接使用（不管 profile 是否匹配，不杀用户已经打开的 Chrome）
    if cdp_url and _cdp_available(cdp_url):
        return session
    # Cached session has dead CDP URL — clean up
    if session:
        try:
            _session_file(profile).unlink(missing_ok=True)
        except Exception:
            pass
    recovered = _find_live_cdp_session_for_profile(profile, session)
    if recovered:
        return recovered

    # No live Chrome found — use chrome_launcher for cross-platform auto-launch.
    # Uses user's DEFAULT Chrome profile (preserves 1688/Ozon login sessions).
    try:
        import logging as _logging
        _logger = _logging.getLogger(__name__)

        # Try chrome_launcher first (preferred: uses default profile)
        try:
            from scripts.lib.chrome_launcher import ensure_chrome_cdp, get_cdp_url
            # Pass skill profile dir so Chrome uses the same profile we check later
            ok, msg = ensure_chrome_cdp(auto_restart=True, profile_dir=str(_profile_dir(profile)))
            if ok:
                cdp_url = get_cdp_url()
                _logger.info("Chrome launched via chrome_launcher: %s", cdp_url)
                session_payload = {
                    'profile': profile,
                    'cdp_url': cdp_url,
                    'created_at': int(time.time()),
                }
                try:
                    _write_browser_session(profile, session_payload)
                except Exception:
                    pass
                return session_payload
            else:
                _logger.warning("chrome_launcher failed: %s, falling back to legacy", msg)
        except ImportError:
            _logger.debug("chrome_launcher not available, using legacy launch")

        # Fallback: legacy launch with separate profile
        from scripts.capabilities.browser_probe.stealth import STEALTH_ARGS
        import random as _random, socket as _sock, subprocess as _sp

        profile_dir = _profile_dir(profile)
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Find an available CDP port
        cdp_port = 9222
        for p in range(9222, 9300):
            try:
                with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
                    s.bind(('127.0.0.1', p))
                    cdp_port = p
                    break
            except OSError:
                continue

        _logger.info("Auto-launching Chrome with profile %s on port %d", profile_dir, cdp_port)

        chrome_bin = find_browser_executable()
        if not chrome_bin:
            _logger.error("Cannot find Chrome executable")
            return {}
        cmd = [
            chrome_bin,
            f'--remote-debugging-port={cdp_port}',
            f'--user-data-dir={profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-background-networking',
            '--disable-sync',
            '--no-pings',
            '--lang=zh-CN',
        ] + STEALTH_ARGS
        if platform.system() == 'Windows':
            _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                      creationflags=_sp.CREATE_NEW_PROCESS_GROUP)
        else:
            _sp.Popen(cmd, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True)
        cdp_url = f'http://127.0.0.1:{cdp_port}'

        # Wait up to 15s for CDP to become ready
        import time as _time
        for _ in range(15):
            _time.sleep(1)
            if _cdp_available(cdp_url):
                _logger.info("Auto-launched Chrome ready at %s", cdp_url)
                return {'cdp_url': cdp_url}
        _logger.warning("Chrome launched but CDP not ready after 15s")
        return {'cdp_url': cdp_url}
    except Exception as e:
        _logger.debug("Auto-launch failed: %s", e)
    return {}


def _connect_existing_chrome(cdp_url: str) -> tuple[CdpConnection, bool]:
    """Connect to an existing Chrome instance via CDP.
    """
    import platform
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    # 1. Try connecting to existing Chrome via CDP (retry up to 3 times)
    for attempt in range(3):
        try:
            conn = CdpConnection(cdp_url)
            # Verify connection by checking version endpoint
            import requests as _req
            resp = _req.get(f"{cdp_url.rstrip('/')}/json/version", timeout=5)
            resp.raise_for_status()
            _logger.info("Connected to existing Chrome at %s", cdp_url)
            return conn, True
        except Exception as exc:
            if attempt < 2:
                import time as _time_cdp
                _time_cdp.sleep(3)
            else:
                _logger.debug("CdpConnection failed after 3 attempts (%s)", exc)

    # 2. Fallback: launch Chrome via chrome_launcher
    try:
        from scripts.lib.chrome_launcher import ensure_chrome_cdp, get_cdp_url
        ok, msg = ensure_chrome_cdp(auto_restart=True)
        if ok:
            new_cdp_url = get_cdp_url()
            _logger.info("Chrome launched via chrome_launcher: %s", new_cdp_url)
            return CdpConnection(new_cdp_url), False
        else:
            _logger.warning("chrome_launcher failed: %s", msg)
    except ImportError:
        _logger.debug("chrome_launcher not available")

    # 3. Fallback: use chrome_launcher (kills existing Chrome first, prevents duplicates)
    try:
        from scripts.lib.chrome_launcher import ensure_chrome_cdp, get_cdp_url
        ok, msg = ensure_chrome_cdp(auto_restart=True, profile_dir=str(_profile_dir('default')))
        if ok:
            cdp_url = get_cdp_url()
            _logger.info("Chrome launched via fallback: %s", cdp_url)
            return CdpConnection(cdp_url), False
        else:
            raise ConfigError(f'Chrome 启动失败: {msg}')
    except ImportError:
        raise ConfigError('未找到 chrome_launcher 模块')


def _open_target_page_in_existing_browser(
    cdp: CdpConnection,
    target_url: str,
    *,
    timeout_seconds: int,
) -> CdpTab:
    from scripts.capabilities.browser_probe.stealth import STEALTH_JS, REALISTIC_UA

    tab = cdp.new_tab()

    # 注入反检测 JS
    tab.add_init_script(STEALTH_JS)

    # UA 覆盖
    tab.set_extra_headers({'User-Agent': REALISTIC_UA})

    timeout_sec = max(int(timeout_seconds), 45)
    tab.navigate(target_url, wait_until='domcontentloaded', timeout=timeout_sec)
    try:
        tab.wait_for_load(timeout=min(timeout_sec, 10))
    except Exception:
        pass

    # 随机延迟，模拟人类阅读
    navigation_delay()

    return tab


def _probe_opened_target_page_with_retries(
    tab: CdpTab,
    target_url: str,
    *,
    timeout_seconds: int,
    poll_ms: int,
    headed: bool,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """Probe with retries on the SAME tab — no page reload.

    Reloading the page (tab.navigate) counts as a new request to 1688
    and triggers rate limiting.  Instead we wait and re-evaluate the
    extraction JS on the already-loaded DOM.
    """
    last_probe: dict[str, Any] = {}
    backoff_schedule_ms = [1500, 3000]
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            wait_ms = backoff_schedule_ms[min(attempt - 2, len(backoff_schedule_ms) - 1)]
            try:
                time.sleep(wait_ms / 1000)
            except Exception:
                pass
            # Scroll to trigger lazy-loading, then re-evaluate JS on the SAME tab
            try:
                tab.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});")
                time.sleep(0.5)
                tab.evaluate("window.scrollTo({top: 0, behavior: 'instant'});")
                time.sleep(0.3)
            except Exception:
                pass

        # Scroll to trigger any remaining lazy images/IntersectionObserver content
        try:
            tab.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'});")
            time.sleep(0.5)
            tab.evaluate("window.scrollTo({top: 0, behavior: 'instant'});")
            time.sleep(0.3)
        except Exception:
            pass
        probe = _read_page_probe(
            tab,
            timeout_seconds=timeout_seconds,
            poll_ms=poll_ms,
            headed=headed,
            allow_slow_fallback=True,
        )
        probe['openAttempt'] = attempt
        last_probe = probe
        # Captcha detected — pause and wait for user to solve it
        if probe.get('captchaDetected') or _looks_like_captcha_intercept(probe):
            print('\n⚠️  检测到 1688 验证码，请在浏览器中手动滑动验证', file=sys.stderr)
            print('   验证完成后按 Enter 继续...', file=sys.stderr)
            try:
                input()
            except EOFError:
                pass
            time.sleep(2)
            continue  # Re-probe immediately
        if probe.get('ready'):
            return probe
        if not (_looks_like_failure_page(probe) or _looks_like_captcha_intercept(probe)):
            return probe
    return last_probe


def _snapshot_login_required(url: str | None, body_text: str | None) -> bool:
    target_url = str(url or '')
    body = str(body_text or '')[:3000]
    return bool(
        ('login.taobao.com' in target_url)
        or ('login.1688.com' in target_url)
        or ('member.1688.com/member/signin_jump' in target_url)
        or ('扫码登录' in body)
        or ('密码登录' in body)
        or ('短信登录' in body)
        or ('登录' in body and '1688' in body)
    )


def _probe_login_snapshot(tab: CdpTab) -> dict[str, Any]:
    try:
        return tab.evaluate("""() => ({
            url: location.href,
            title: document.title || '',
            bodyText: (document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 4000)
        })""")
    except Exception as exc:
        return {'url': getattr(tab, 'url', None), 'title': '', 'bodyText': '', 'error': str(exc)}


def _extract_qr_code_base64(tab: CdpTab) -> str | None:
    """Extract 1688 login QR code from canvas as base64 data URL."""
    try:
        return tab.evaluate("""() => {
            const canvas = document.querySelector('.qrcode-img canvas');
            if (!canvas) return null;
            try {
                return canvas.toDataURL('image/png');
            } catch(e) { return null; }
        }""")
    except Exception:
        return None


def _wait_for_login_session(
    target_url: str,
    *,
    profile_name: str,
    browser_path: str,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    """Connect via CDP and wait for 1688 login.

    Uses CdpConnection to connect to Chrome via CDP and open a new tab
    for the login page. After login is detected, the browser stays running
    so subsequent CDP probes can reuse it.

    If a login is already in progress, waits for it to complete instead
    of starting a second concurrent login flow.
    """
    global _login_in_progress, _login_result
    import logging as _logging
    import base64 as _base64
    _logger = _logging.getLogger(__name__)

    # Prevent double login: if another thread is already waiting, block until it finishes
    if _login_in_progress:
        _logger.info("Login already in progress, waiting for it to complete...")
        _login_done_event.wait(timeout=max(timeout_seconds, 60))
        return _login_result

    # ✅ 修复：使用 flag 标记是否需要释放锁，避免 double-release
    _login_lock.acquire()
    _should_release = True
    try:
        # Double-check after acquiring lock
        if _login_in_progress:
            _logger.info("Login already in progress (race), waiting...")
            _login_lock.release()
            _should_release = False
            try:
                _login_done_event.wait(timeout=max(timeout_seconds, 60))
            finally:
                _login_lock.acquire()
                _should_release = True
            return _login_result

        _login_in_progress = True
        _login_done_event.clear()
        _login_result = None

        # ... rest of login logic inside try block ...
    finally:
        if _should_release:
            _login_lock.release()

    from scripts.capabilities.browser_probe.stealth import STEALTH_JS, REALISTIC_UA

    session = _resolve_browser_session(profile_name)
    login_url = 'https://login.1688.com/member/signin.htm'
    timeout_sec = max(timeout_seconds, 30)
    start = time.time()

    tab = None
    try:
        # Find existing Chrome CDP session
        session = _resolve_browser_session(profile_name)
        cdp_url = session.get('cdp_url', '')
        if not cdp_url:
            recovered = _find_live_cdp_session_for_profile(profile_name, session)
            if recovered:
                cdp_url = str(recovered.get('cdp_url') or '')
        if not cdp_url:
            _logger.error("No Chrome CDP session found — cannot wait for login")
            return None

        # Save session info
        session['cdp_url'] = cdp_url
        try:
            _write_browser_session(profile_name, session)
        except Exception:
            pass

        # Connect to existing Chrome via CDP
        _logger.info("Connecting to Chrome CDP at %s", cdp_url)
        cdp = CdpConnection(cdp_url)
        tab = cdp.new_tab()

        # 注入反检测 JS
        tab.add_init_script(STEALTH_JS)

        # UA 覆盖
        tab.set_extra_headers({'User-Agent': REALISTIC_UA})

        # Navigate to login page
        _logger.info("Navigating to 1688 login page...")
        tab.navigate(login_url, wait_until='domcontentloaded', timeout=20)
        sleep_random(2000, 4000)  # Wait for QR code canvas to render

        # Extract QR code
        qr_data = _extract_qr_code_base64(tab)
        if qr_data and qr_data.startswith('data:image/'):
            header, img_data = qr_data.split(',', 1)
            try:
                import tempfile
                qr_path = Path(tempfile.gettempdir()) / '1688_qrcode.png'
                qr_path.write_bytes(_base64.b64decode(img_data))
                _logger.info("QR code saved to %s", qr_path)
            except Exception:
                qr_path = None

            print(f'\n📱 请用手机 1688/淘宝 App 扫下方二维码登录：\n',
                  file=sys.stderr)
            print(f'  🔗 data:image/png;base64,{img_data[:40]}...',
                  file=sys.stderr)
            if qr_path:
                print(f'\n  或保存 {qr_path} 用手机扫描。\n',
                      file=sys.stderr)
        else:
            _logger.warning("Cannot extract QR code from 1688 login page")
            print(f'\n⛔ 无法提取 1688 登录二维码，请手动访问扫码:\n'
                  f'   {login_url}\n', file=sys.stderr)

        # Poll for login completion
        while time.time() - start < timeout_sec:
            try:
                snapshot = _probe_login_snapshot(tab)
                login_required = _snapshot_login_required(snapshot.get('url'), snapshot.get('bodyText'))
                if not login_required:
                    merged = dict(session)
                    merged['login_detected'] = True
                    merged['login_check_url'] = snapshot.get('url')
                    _logger.info("1688 login detected at %s", snapshot.get('url'))
                    _login_result = merged
                    return merged
            except Exception:
                pass
            time.sleep(3)

        _logger.warning("_wait_for_login_session: login timeout after %ds", timeout_sec)
        return None
    except Exception as exc:
        _logger.error("_wait_for_login_session: CDP error: %s", exc)
        return None
    finally:
        if tab:
            try:
                tab.close()
            except Exception:
                pass
        # Close CDP connection to prevent WebSocket leak
        try:
            cdp.close()
        except Exception:
            pass
        _login_in_progress = False
        _login_done_event.set()


def _check_1688_login_live(cdp_url: str) -> bool:
    """Check 1688 login status by navigating to a product page.

    Instead of reading cookies (which are often HttpOnly and invisible to JS),
    we navigate to a known product page and check if product content loads.
    If redirected to login page, login is expired.
    """
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    if not cdp_url or not _cdp_available(cdp_url):
        return False

    conn = None
    temp_tab = None
    try:
        conn = CdpConnection(cdp_url)
        temp_tab = conn.new_tab()

        # 注入反检测脚本（防止1688滑块验证码）
        from scripts.capabilities.browser_probe.stealth import STEALTH_JS, REALISTIC_UA
        temp_tab.add_init_script(STEALTH_JS)
        temp_tab.set_extra_headers({'User-Agent': REALISTIC_UA})

        # Navigate to a product page (any 1688 product)
        try:
            temp_tab.navigate(
                "https://detail.1688.com/offer/770530889059.html",
                wait_until='domcontentloaded', timeout=15,
            )
            temp_tab.wait_for_load(timeout=5)
        except Exception:
            pass
        time.sleep(1)

        # Check if product content loaded (not redirected to login)
        try:
            url = temp_tab.evaluate("() => window.location.href", timeout=5) or ""
            title_el = temp_tab.evaluate(
                "document.querySelector('.title-content')?.innerText || ''",
                timeout=5,
            ) or ""

            # If redirected to login or title is empty (login wall), not logged in
            if "login.1688.com" in url or "signin" in url:
                _logger.debug("_check_1688_login_live: redirected to login page")
                return False
            if not title_el:
                _logger.debug("_check_1688_login_live: product title not found (possible login wall)")
                return False

            _logger.info("_check_1688_login_live: login OK (product title: %s)", title_el[:30])
            return True
        except Exception as exc:
            _logger.debug("_check_1688_login_live: page check failed: %s", exc)
            return False
    except Exception as exc:
        _logger.debug("_check_1688_login_live: CDP failed: %s", exc)
        return False
    finally:
        if temp_tab:
            try:
                temp_tab.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _read_page_probe(
    tab: CdpTab,
    *,
    timeout_seconds: int,
    poll_ms: int,
    headed: bool,
    allow_slow_fallback: bool = True,
) -> dict[str, Any]:
    initial = _single_pass_probe(tab)
    if initial.get('ready'):
        initial['probe_mode'] = 'single_pass'
        return initial
    if _looks_like_captcha_intercept(initial) or _looks_like_failure_page(initial):
        initial['probe_mode'] = 'single_pass_terminal'
        return initial
    if bool(initial.get('loginRequired')):
        if headed and allow_slow_fallback:
            polled = _poll_probe(tab, timeout_seconds=timeout_seconds, poll_ms=poll_ms, headed=headed)
            polled['probe_mode'] = 'poll_after_login_gate'
            return polled
        initial['probe_mode'] = 'single_pass_login_gate'
        return initial
    if not allow_slow_fallback:
        initial['probe_mode'] = 'single_pass_no_fallback'
        return initial
    if initial.get('title') or len(initial.get('images') or []) > 0:
        initial['probe_mode'] = 'single_pass_partial'
        return initial
    polled = _poll_probe(tab, timeout_seconds=timeout_seconds, poll_ms=poll_ms, headed=headed)
    polled['probe_mode'] = 'poll_fallback'
    return polled

def _artifact_path(task_id: str | None = None) -> Path:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    suffix = uuid.uuid4().hex[:8]
    return task_media_dir('browser-probes', task_id=task_id) / f'1688-probe-{stamp}-{suffix}.json'


def _cache_key(url: str) -> str:
    """Normalize a 1688 URL for cache lookup — strip query params, keep offer ID."""
    m = re.search(r'offer/(\d+)', url)
    if m:
        return f'1688-offer-{m.group(1)}'
    return url.split('?')[0].rstrip('/')


def _find_cached_probe(target_url: str, task_id: str | None = None, max_age_seconds: int = 86400) -> dict[str, Any] | None:
    """Look for a cached browser probe result for the same 1688 offer.

    Scans browser-probes directory for JSON artifacts containing the same URL.
    Returns cached result if found and not older than max_age_seconds.
    """
    probes_dir = task_media_dir('browser-probes', task_id=task_id)
    if not probes_dir.is_dir():
        return None
    key = _cache_key(target_url)
    newest_mtime = 0.0
    newest_path = None
    for f in sorted(probes_dir.glob('1688-probe-*.json'), reverse=True):
        try:
            stat = f.stat()
            age = time.time() - stat.st_mtime
            if age > max_age_seconds:
                continue
            # Quick check: look for the URL in the first few KB without full parse
            head = f.read_text(encoding='utf-8')[:4096]
            if key in head or target_url.split('?')[0] in head:
                if stat.st_mtime > newest_mtime:
                    newest_mtime = stat.st_mtime
                    newest_path = f
        except Exception:
            continue
    if newest_path is None:
        return None
    try:
        cached = json.loads(newest_path.read_text(encoding='utf-8'))
        if cached.get('ready') and not cached.get('failure_page') and not cached.get('captcha_intercepted'):
            cached['from_cache'] = True
            cached['cache_age_seconds'] = round(time.time() - newest_mtime, 1)
            return cached
    except Exception:
        pass
    return None


def _filter_probe_images(images: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in images or []:
        value = str(raw or '').strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    filtered = [url for url in deduped if is_likely_product_image(url)]
    # 排序：主产品图在前（白底图 > 1688图 > 其它），gg_dtc缩略图已被 is_likely_product_image 过滤
    from scripts.lib.reference_images import reference_priority
    scored = [(reference_priority(url), url) for url in filtered]
    scored.sort(key=lambda x: x[0])
    return [url for _, url in scored] or deduped


def _auto_install_browser() -> bool:
    """Automatically install Playwright Chromium when no browser is found.

    Returns True if a browser became available after installation.
    Uses npmmirror.com (国内镜像) for both pip and Playwright downloads.
    Does NOT prompt the user — fully automatic.
    """
    import subprocess as _sp
    python = _sp.sys.executable or 'python3'

    # 国内镜像加速 — use npmmirror for pip and playwright downloads
    mirror_env = {**_sp.os.environ,
        'PLAYWRIGHT_DOWNLOAD_HOST': 'https://npmmirror.com/mirrors/playwright/',
        'PIP_INDEX_URL': 'https://pypi.tuna.tsinghua.edu.cn/simple',
    }

    # Step 1: ensure playwright package is installed
    try:
        import playwright  # noqa: F401
    except ImportError:
        try:
            _sp.run(
                [python, '-m', 'pip', 'install', 'playwright', '-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'],
                check=True, capture_output=True, timeout=120, env=mirror_env,
            )
        except Exception:
            # Fallback: try without mirror
            try:
                _sp.run(
                    [python, '-m', 'pip', 'install', 'playwright'],
                    check=True, capture_output=True, timeout=120,
                )
            except Exception:
                return False

    # Step 2: install Chromium browser (with China mirror)
    try:
        _sp.run(
            [python, '-m', 'playwright', 'install', 'chromium'],
            check=True, capture_output=True, timeout=300, env=mirror_env,
        )
    except Exception:
        # Fallback: try without mirror
        try:
            _sp.run(
                [python, '-m', 'playwright', 'install', 'chromium'],
                check=True, capture_output=True, timeout=300,
            )
        except Exception:
            return False

    # Step 3: re-scan for the newly installed browser
    return bool(find_browser_executable(None))


def check_cdp_prerequisites(
    profile: str | None = None,
    browser_path: str | None = None,
) -> dict[str, Any]:
    """Check whether CDP browser probe can run.

    Returns:
        {
            'ok': bool,
            'browser_available': bool,
            'browser_path': str | None,
            'session_available': bool,
            'cdp_url': str | None,
            'login_required': bool,
            'issues': list[str],       # human-readable problems
            'suggestions': list[str],  # actionable next steps
        }

    Call this BEFORE probe_1688_page() to give the user clear guidance.
    Does NOT launch a browser or wait for login.
    """
    profile_name = str(profile or get_config_profile() or 'default').strip() or 'default'
    issues: list[str] = []
    suggestions: list[str] = []

    # 0. Check if we're in a headless environment (no display at all)
    import os as _os
    _is_headless = False
    if _os.name == 'nt':
        # Windows: SESSIONNAME 不存在说明是服务/CI 环境（无桌面）
        _is_headless = not _os.environ.get('SESSIONNAME')
    elif _os.uname().sysname == 'Darwin':
        import subprocess as _sp
        try:
            r = _sp.run(['pgrep', '-x', 'WindowServer'], capture_output=True, timeout=2)
            _is_headless = r.returncode != 0
        except Exception:
            pass
    else:
        _is_headless = not (_os.environ.get('DISPLAY') or _os.environ.get('WAYLAND_DISPLAY'))
    if _is_headless:
        return {
            'ok': False, 'browser_available': False, 'browser_path': None,
            'session_available': False, 'cdp_url': None, 'login_required': True,
            'issues': ['无图形界面，无法启动浏览器'],
            'suggestions': ['在桌面环境运行 publish-new 以启用 CDP 富集'],
        }

    # 1. Check browser
    resolved_browser = find_browser_executable(browser_path)
    browser_available = bool(resolved_browser)

    # ═══════════════════════════════════════════════════════════════════════
    # Auto-install Playwright Chromium when no system browser is found.
    # This is fast if Playwright is already installed — just a file-exists
    # check in Phase 3.  Only does pip install on first run.
    # ═══════════════════════════════════════════════════════════════════════
    if not browser_available:
        import logging as _logging
        _logger = _logging.getLogger(__name__)
        _logger.info('No Chromium browser found — attempting auto-install via Playwright...')
        if _auto_install_browser():
            resolved_browser = find_browser_executable(None)
            browser_available = bool(resolved_browser)
            if browser_available:
                _logger.info('Playwright Chromium installed successfully: %s', resolved_browser)
                # Clear issues — we have a browser now
                issues.clear()
                suggestions.clear()

    if not browser_available:
        import platform
        system = platform.system()
        if system == 'Darwin':
            issues.append('未找到 Chrome/Chromium 浏览器')
            suggestions.append('方案 A: 安装 Google Chrome — https://www.google.com/chrome/')
            suggestions.append('方案 B: pip install playwright && playwright install chromium (自带浏览器)')
        elif system == 'Linux':
            issues.append('未找到 Chrome/Chromium 浏览器')
            suggestions.append('方案 A: sudo apt install chromium-browser  或  google-chrome-stable')
            suggestions.append('方案 B: pip install playwright && playwright install chromium (自带浏览器)')
        elif system == 'Windows':
            issues.append('未找到 Chrome/Chromium 浏览器')
            suggestions.append('方案 A: 安装 Google Chrome — https://www.google.com/chrome/')
            suggestions.append('方案 B: 安装 Microsoft Edge (系统已内置或从 microsoft.com/edge 下载)')
            suggestions.append('方案 C: pip install playwright && playwright install chromium (自带浏览器)')
        else:
            issues.append('未找到 Chrome/Chromium 浏览器')
            suggestions.append('请安装 Google Chrome: https://www.google.com/chrome/')
            suggestions.append('或运行: pip install playwright && playwright install chromium')

    # 2. Check CDP session
    session = _resolve_browser_session(profile_name)
    cdp_url = str(session.get('cdp_url') or '').strip()
    session_available = bool(cdp_url and _cdp_available(cdp_url))

    if browser_available and not session_available:
        issues.append('没有可连接的 1688 浏览器会话')
        suggestions.append('直接运行 publish-new，首次会自动打开 Chrome 登录页')
        suggestions.append('或手动启动 Chrome 并登录 1688:')
        suggestions.append(
            f'  Chrome 需带参数: --remote-debugging-port=9222 --user-data-dir=<profile目录>'
        )

    # 3. Check login — use saved session flag, don't open new pages (CDP-incompatible)
    login_required = True
    if session_available:
        login_required = not bool(session.get('login_detected'))

    if session_available and login_required:
        issues.append('浏览器会话未登录 1688')
        suggestions.append('请在浏览器中登录 1688: https://login.1688.com/member/signin.htm')

    return {
        'ok': browser_available and session_available and not login_required,
        'browser_available': browser_available,
        'browser_path': resolved_browser,
        'session_available': session_available,
        'cdp_url': cdp_url if session_available else None,
        'login_required': login_required,
        'issues': issues,
        'suggestions': suggestions,
    }


def probe_1688_page_safe(
    url: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Call probe_1688_page() with graceful error handling.

    Never raises — returns a result dict with `ok`, `data`, `error`, `degraded`.
    Use this as the primary entry point from agent flows (Worker A Step 2b).

    Detects asyncio event loop and runs Playwright sync code in a separate
    thread to avoid "Playwright Sync API inside the asyncio loop" errors.
    """
    # Check if we're inside an asyncio event loop
    _in_async = False
    try:
        _in_async = asyncio.get_running_loop() is not None
    except RuntimeError:
        pass

    def _do_probe():
        try:
            result = probe_1688_page(url, **kwargs)
            probe = result.get('probe', {})
            return {
                'ok': result.get('ready', False),
                'degraded': not result.get('ready', False),
                'data': {
                    'title': probe.get('title', ''),
                    'price': probe.get('price', ''),
                    'brand': probe.get('brand', ''),
                    'seller': probe.get('seller', ''),
                    'images': _filter_probe_images(list(probe.get('images') or [])),
                    'weight_grams': (probe.get('packagingRows') or [{}])[0].get('weightGrams') if probe.get('packagingRows') else None,
                    'packaging_rows': probe.get('packagingRows', []),
                    'shipping': probe.get('shipping', {}),
                    'description': probe.get('description', ''),
                    'sku_details': probe.get('skuDetails', []),
                    'attributes': probe.get('attributes', []),
                    'option_groups': probe.get('optionGroups', []),
                },
                'error': None if result.get('ready') else '页面数据未完全提取，部分字段可能缺失',
                'raw': result,
            }
        except Exception as exc:
            return {
                'ok': False,
                'degraded': True,
                'data': {
                    'title': '', 'price': '', 'brand': '', 'seller': '',
                    'images': [], 'weight_grams': None,
                    'packaging_rows': [], 'shipping': {},
                    'description': '',
                    'sku_details': [], 'attributes': [], 'option_groups': [],
                },
                'error': str(exc),
                'raw': {},
            }

    if _in_async:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_do_probe)
            return future.result(timeout=kwargs.get('timeout_seconds', 120) + 30)
    else:
        return _do_probe()


def probe_1688_page(
    url: str,
    *,
    headed: bool = False,
    timeout_seconds: int = 120,
    poll_ms: int = 1500,
    profile: str | None = None,
    browser_path: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    target_url = str(url or '').strip()
    if not target_url:
        raise ValidationError('1688 页面 URL 不能为空')
    if '1688.com' not in target_url:
        raise ValidationError('browser_probe 当前只支持 1688 页面 URL')

    # Cache-aside: reuse cached probe result if fresh (avoid 1688 rate limiting)
    cached = _find_cached_probe(target_url, task_id, max_age_seconds=_CACHE_TTL)
    if cached is not None:
        cached['artifact_path'] = str(_artifact_path(task_id or current_task_id()))
        return cached

    # Pre-probe random delay: simulate human browsing pace, avoid rate limiting
    pre_delay_ms = random.randint(2000, 5000)
    time.sleep(pre_delay_ms / 1000.0)

    resolved_browser = find_browser_executable(browser_path)
    if not resolved_browser:
        raise ConfigError('未找到可用的 Chrome/Chromium 浏览器，请先安装 Google Chrome 或传入 --browser-path')
    profile_name = str(profile or get_config_profile() or 'default').strip() or 'default'
    user_data_dir = _profile_dir(profile_name)
    artifact = _artifact_path(task_id or current_task_id())
    launch_meta = {
        'browser_path': resolved_browser,
        'profile': profile_name,
        'user_data_dir': str(user_data_dir),
        'headed': bool(headed),
        'timeout_seconds': int(timeout_seconds),
        'poll_ms': int(poll_ms),
        'attach_only': False,
        'auto_open_disabled': False,
    }
    session = _resolve_browser_session(profile_name)
    cdp_url = str(session.get('cdp_url') or '').strip()
    if not cdp_url or not _cdp_available(cdp_url):
        session = _wait_for_login_session(
            target_url,
            profile_name=profile_name,
            browser_path=resolved_browser,
            timeout_seconds=timeout_seconds,
        ) or {}
        cdp_url = str(session.get('cdp_url') or '').strip()
        launch_meta['session_bootstrapped'] = bool(cdp_url)
    if not cdp_url or not _cdp_available(cdp_url):
        raise ConfigError('未发现可复用的 1688 浏览器会话，请先执行 browser_login 完成登录，或保持同一 profile 的 Chrome 会话可连接')

    # Live cookie check: verify 1688 login before navigating to product page
    try:
        login_ok = _check_1688_login_live(cdp_url)
    except ConnectionError:
        # CDP 连接断开（Chrome 死了或 tab 被关）— 重启 Chrome 而非触发登录
        import logging as _logging
        _logging.getLogger(__name__).warning("CDP connection died during login check, restarting Chrome...")
        session = _resolve_browser_session.__wrapped__(profile_name) if hasattr(_resolve_browser_session, '__wrapped__') else _resolve_browser_session(profile_name)
        cdp_url = str(session.get('cdp_url') or '').strip()
        if not cdp_url or not _cdp_available(cdp_url):
            raise ConfigError('Chrome CDP 连接断开且无法重启，请手动重启 Chrome')
        login_ok = True  # 新 Chrome session 用已有 profile，cookie 有效

    if not login_ok:
        import logging as _logging
        _logging.getLogger(__name__).info("1688 login not detected via live cookie check, prompting login...")
        session = _wait_for_login_session(
            target_url,
            profile_name=profile_name,
            browser_path=resolved_browser,
            timeout_seconds=timeout_seconds,
        ) or session
        cdp_url = str(session.get('cdp_url') or cdp_url).strip()
        if not cdp_url or not _cdp_available(cdp_url):
            raise ConfigError('1688 登录未完成，无法继续探测')

    cdp, connected_to_existing = _connect_existing_chrome(cdp_url)
    opened_tab: CdpTab | None = None
    try:
        # Find existing tab with matching offer
        offer_id = _extract_offer_id(target_url)
        matched_existing_tab = None
        if offer_id:
            matched_existing_tab = cdp.find_tab(offer_id)
        if not matched_existing_tab:
            tmp = cdp.find_tab("1688.com")
            if tmp and _page_matches_target_offer(tmp, target_url):
                matched_existing_tab = tmp
            elif tmp:
                # Wrong tab — close the browser tab we found (it's not the one user wants)
                try:
                    tmp.close()
                except Exception:
                    pass

        tab = matched_existing_tab
        opened_page = False
        if tab is None:
            tab = _open_target_page_in_existing_browser(
                cdp,
                target_url,
                timeout_seconds=timeout_seconds,
            )
            opened_tab = tab
            opened_page = True
            probe = _probe_opened_target_page_with_retries(
                tab,
                target_url,
                timeout_seconds=timeout_seconds,
                poll_ms=poll_ms,
                headed=bool(headed),
            )
            probe['noMatchingOpenPage'] = True
        else:
            probe = _read_page_probe(
                tab,
                timeout_seconds=timeout_seconds,
                poll_ms=poll_ms,
                headed=bool(headed),
                allow_slow_fallback=True,
            )
            probe['noMatchingOpenPage'] = False
        launch_meta['cdp_url'] = cdp_url
        launch_meta['connected_existing_chrome'] = True
        launch_meta['matched_existing_page'] = bool(matched_existing_tab)
        launch_meta['auto_opened_page'] = bool(opened_page)
        launch_meta['login_detected'] = bool(session.get('login_detected'))
        launch_meta['login_check_url'] = session.get('login_check_url')
        if opened_page:
            launch_meta['auto_open_probe_attempts'] = int(probe.get('openAttempt') or 1)
            try:
                tab.close()
            except Exception:
                pass
    except Exception as exc:
        raise ConfigError(f'浏览器探测失败: {exc}') from exc
    finally:
        # Only close tabs we opened; never close user's existing Chrome tabs
        if opened_tab and not opened_tab._closed:
            try:
                opened_tab.close()
            except Exception:
                pass
        # Close CDP connection to prevent WebSocket leak
        try:
            cdp.close()
        except Exception:
            pass
    result = {
        'ready': bool(probe.get('ready')),
        'timed_out': bool(probe.get('timed_out')),
        'failure_page': _looks_like_failure_page(probe),
        'captcha_intercepted': _looks_like_captcha_intercept(probe),
        'no_matching_open_page': bool(probe.get('noMatchingOpenPage')),
        'launch': launch_meta,
        'summary': _build_summary(probe),
        'probe': probe,
        'artifact_path': str(artifact),
    }
    probe['images'] = _filter_probe_images(list(probe.get('images') or []))
    result['summary'] = _build_summary(probe)
    artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
