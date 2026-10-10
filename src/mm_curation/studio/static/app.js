/* Studio 前端逻辑。刻意不用框架 —— 本地工具，少一层构建就少一类起不来的原因。 */
'use strict';

const S = {
  session: 's' + Math.random().toString(36).slice(2, 10),
  scenarios: [],
  picked: null,
  checked: false,
  lastFunnel: null,
  pollTimer: null,
};

const $ = (id) => document.getElementById(id);
const COST_LABEL = { rule: '规则', perceptual: '感知', model: '模型', llm: '大模型' };

function toast(msg, isErr) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast on' + (isErr ? ' err' : '');
  clearTimeout(toast._h);
  toast._h = setTimeout(() => { t.className = 'toast'; }, isErr ? 6500 : 3000);
}

function goto(n) {
  for (let i = 1; i <= 4; i++) {
    $('panel' + i).classList.toggle('on', i === n);
  }
  document.querySelectorAll('.step').forEach((el) => {
    const k = +el.dataset.n;
    el.classList.toggle('active', k === n);
    el.classList.toggle('done', k < n);
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

async function api(path, body) {
  const opt = { method: body ? 'POST' : 'GET' };
  if (body) {
    opt.headers = { 'Content-Type': 'application/json' };
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ ok: false, error: '服务器返回的不是 JSON（可能崩了）' }));
  if (!j.ok) {
    const e = new Error(j.error || '未知错误');
    e.hint = j.hint || '';
    throw e;
  }
  return j;
}

function showErr(id, e) {
  const msg = e.hint ? `${e.message}<br><span style="opacity:.8">建议：${e.hint}</span>` : e.message;
  $(id).innerHTML = `<div class="res err"><h4>这一步没通过</h4>${escapeHtml(msg).replace(/&lt;br&gt;/g, '<br>')}</div>`;
  toast(e.message, true);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ── 1. 场景卡 ───────────────────────────────────────── */
async function loadScenarios() {
  try {
    const j = await api('/api/scenarios');
    S.scenarios = j.scenarios;
    renderCards();
  } catch (e) {
    $('cards').innerHTML = `<div class="res err">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderCards() {
  const box = $('cards');
  box.innerHTML = '';
  for (const sc of S.scenarios) {
    const d = document.createElement('div');
    d.className = 'card' + (S.picked === sc.key ? ' sel' : '');
    // 显示**中文说明**（label），不是算子英文名 ——
    // 产品经理看到 `text_minhash` 只会以为页面坏了。英文名放进 title 备查。
    const stepTags = sc.steps.map((s) =>
      `<span class="tag ${s.cost}" title="${escapeHtml(s.op)}：${escapeHtml(s.label)}">`
      + `${escapeHtml(s.label)}<em>${escapeHtml(s.cost_zh || COST_LABEL[s.cost] || s.cost)}</em>`
      + `</span>`).join('');
    d.innerHTML = `
      <h3>${escapeHtml(sc.title)}</h3>
      <p class="blurb">${escapeHtml(sc.blurb)}</p>
      <p class="meta">产出：${escapeHtml(sc.produces)}</p>
      <p class="meta">${sc.n_steps} 道检查 · 必填字段：
        ${sc.required_fields.map((f) => `<code>${escapeHtml(f)}</code>`).join(' ')}</p>
      ${sc.cost_note ? `<p class="meta">⚠ ${escapeHtml(sc.cost_note)}</p>` : ''}
      <p class="steps">${stepTags}</p>
      <p class="meta">${escapeHtml(sc.sample_hint)}</p>
      ${sc.caveats.length ? '<ul class="caveats">' +
        sc.caveats.map((c) => `<li>${escapeHtml(c)}</li>`).join('') + '</ul>' : ''}
    `;
    d.onclick = () => {
      S.picked = sc.key;
      S.checked = false;
      renderCards();
      showFormat(sc);
      goto(2);
    };
    box.appendChild(d);
  }
}

function showFormat(sc) {
  const needsImg = sc.required_fields.includes('image_path');
  $('imgRow').style.display = needsImg ? '' : 'none';
  $('fmt').innerHTML = `
    <b>${escapeHtml(sc.title)}</b> 需要这些字段：${sc.required_fields.map((f) => `<code>${escapeHtml(f)}</code>`).join(' ')}
    <p class="meta" style="margin-top:6px">文件里每行一条数据，格式长这样：</p>
    <code>${escapeHtml(sc.sample_hint)}</code>
    <p class="meta" style="margin-top:6px">
      支持 <b>.jsonl</b>（推荐，每行一个 JSON）和 <b>.csv</b>。
      ${needsImg ? '图片请把整个图片目录<b>压成 zip</b> 一起上传。' : ''}
    </p>`;
}

/* ── 2. 上传 + 检查 ──────────────────────────────────── */
$('dataFile').onchange = (e) => {
  $('dataName').textContent = e.target.files[0]?.name || '未选择';
  S.checked = false;
};
$('imgFile').onchange = (e) => {
  $('imgName').textContent = e.target.files[0]?.name || '未选择';
};
$('trialChk').onchange = (e) => {
  $('trialN').disabled = !e.target.checked;
};

$('btnCheck').onclick = async () => {
  const f = $('dataFile').files[0];
  if (!f) return toast('先选一个数据文件', true);
  const fd = new FormData();
  fd.append('session', S.session);
  fd.append('data', f, f.name);
  const imf = $('imgFile').files[0];
  if (imf) fd.append('images', imf, imf.name);

  $('checkResult').innerHTML = '<div class="res warn">正在检查…</div>';
  $('btnCheck').disabled = true;
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const up = await r.json();
    if (!up.ok) throw Object.assign(new Error(up.error), { hint: up.hint || '' });
    const ck = await api('/api/check', {
      session: S.session, filename: up.data, scenario: S.picked,
      images_uploaded: !!up.images_uploaded,
    });
    S.checked = true;
    const notes = ck.notes.length
      ? '<ul>' + ck.notes.map((n) => `<li>${escapeHtml(n)}</li>`).join('') + '</ul>' : '';
    $('checkResult').innerHTML = `
      <div class="res ok"><h4>文件可以用 ✓</h4>
        共 <b>${ck.n_rows.toLocaleString()}</b> 条数据${ck.n_bad_json ? `（${ck.n_bad_json} 行无法解析，将被跳过）` : ''}
        ${notes}
      </div>`;
    showRecipe();
    goto(3);
  } catch (e) {
    S.checked = false;
    showErr('checkResult', e);
  } finally {
    $('btnCheck').disabled = false;
  }
};

function showRecipe() {
  const sc = S.scenarios.find((x) => x.key === S.picked);
  const byCost = {};
  for (const s of sc.steps) (byCost[s.cost] ||= []).push(s.op);
  const parts = Object.entries(byCost).map(
    ([c, ops]) => `<b>${COST_LABEL[c] || c}档</b>：${ops.map((o) => `<code>${escapeHtml(o)}</code>`).join(' ')}`
  );
  $('recipeBox').innerHTML = `
    <div class="summary">
      <b>将要执行的 ${sc.n_steps} 道检查</b>
      <p class="meta" style="margin-top:6px">${parts.join('<br>')}</p>
      <p class="meta" style="margin-top:8px">
        每条数据会被逐级判断，任何一道不通过就被丢弃 —— 所以最终数据量会少于上传量。
      </p>
    </div>`;
}

/* ── 3. 跑清洗 ─────────────────────────────────────── */
$('btnRun').onclick = async () => {
  if (!S.checked) return toast('请先完成第 2 步的文件检查', true);
  const limit = $('trialChk').checked ? +$('trialN').value : 0;
  $('runResult').innerHTML = '<div class="res warn">正在提交…</div>';
  $('btnRun').disabled = true;
  try {
    const j = await api('/api/funnel', {
      session: S.session,
      filename: $('dataFile').files[0].name,
      scenario: S.picked,
      limit,
    });
    pollJob(j.id, renderFunnel, 'runResult');
  } catch (e) {
    showErr('runResult', e);
  } finally {
    $('btnRun').disabled = false;
  }
};

function pollJob(id, onDone, boxId) {
  clearInterval(S.pollTimer);
  const tick = async () => {
    try {
      const j = await api('/api/job?id=' + encodeURIComponent(id));
      const job = j.job;
      const pct = Math.round((job.progress || 0) * 100);
      $(boxId).innerHTML = `
        <div class="res warn">
          <h4>${escapeHtml(job.stage || '处理中')} · ${pct}%</h4>
          <div class="prog"><i style="width:${pct}%"></i></div>
          ${job.elapsed_s ? `<p class="meta">已用 ${job.elapsed_s} 秒</p>` : ''}
          ${job.log.length ? `<div class="log">${escapeHtml(job.log.slice(-6).join('\n'))}</div>` : ''}
        </div>`;
      if (job.status === 'done') {
        clearInterval(S.pollTimer);
        onDone(job);
      } else if (job.status === 'failed') {
        clearInterval(S.pollTimer);
        $(boxId).innerHTML = `<div class="res err"><h4>清洗失败</h4>
          ${escapeHtml(job.error)}${job.hint ? '<p class="meta" style="margin-top:6px">建议：' + escapeHtml(job.hint) + '</p>' : ''}</div>`;
        toast('清洗失败，看红色说明', true);
      }
    } catch (e) {
      clearInterval(S.pollTimer);
      showErr(boxId, e);
    }
  };
  tick();
  S.pollTimer = setInterval(tick, 1200);
}

function renderFunnel(job) {
  S.lastFunnel = job.result;
  const r = job.result;
  const rows = r.stages.map((s) => `
    <tr class="${s.dropped ? 'drop' : ''}">
      <td><code>${escapeHtml(s.op)}</code></td>
      <td class="n">${s.n_in.toLocaleString()}</td>
      <td class="n">${s.n_out.toLocaleString()}</td>
      <td class="n">${s.dropped.toLocaleString()}</td>
      <td class="n">${(s.pass_rate * 100).toFixed(1)}%</td>
    </tr>`).join('');
  const droppedBy = {};
  for (const s of r.stages) droppedBy[s.op] = s.dropped;
  const worst = Object.entries(droppedBy).sort((a, b) => b[1] - a[1])[0];

  $('runResult').innerHTML = `
    <div class="res ok"><h4>清洗完成 ✓</h4>
      <div class="stats">
        <div class="stat"><div class="v">${r.n_input.toLocaleString()}</div><div class="k">输入</div></div>
        <div class="stat"><div class="v">${r.n_kept.toLocaleString()}</div><div class="k">保留</div></div>
        <div class="stat"><div class="v">${r.n_dropped.toLocaleString()}</div><div class="k">丢弃</div></div>
        <div class="stat"><div class="v">${(r.keep_rate * 100).toFixed(1)}%</div><div class="k">保留率</div></div>
      </div>
      ${worst && worst[1] > 0 ? `<p class="meta">丢弃最多的是 <code>${escapeHtml(worst[0])}</code>
        （${worst[1].toLocaleString()} 条）—— 如果这个数字大得超出预期，可能需要调阈值。</p>` : ''}
      <table><tr><th>检查项</th><th>进入</th><th>留下</th><th>丢弃</th><th>通过率</th></tr>${rows}</table>
    </div>`;
  renderCleanSummary();
  goto(4);
}

async function renderCleanSummary() {
  if (!S.lastFunnel) return;
  const s = S.lastFunnel;
  let rows = '';
  try {
    const j = await api(`/api/preview?session=${S.session}&limit=8`);
    rows = j.rows.map((r) => `<div class="prev">
        <span class="id">${escapeHtml(r.id || '—')}</span>
        <span>${escapeHtml(r.text) || '<i style="color:#9aa">（无文字）</i>'}</span></div>`).join('');
  } catch (e) { /* 预览失败不阻塞主流程 */ }
  $('cleanSummary').innerHTML = `
    <b>清洗后剩 ${s.n_kept.toLocaleString()} 条</b>，下面是随机看几条：
    ${rows || '<p class="meta">（预览加载失败，不影响生成数据集）</p>'}`;
}

/* ── 4. 生成数据集 ──────────────────────────────────── */
$('btnBuild').onclick = async () => {
  const name = $('dsName').value.trim();
  if (!/^[A-Za-z0-9_-]+$/.test(name)) {
    return toast('数据集名只能用字母、数字、连字符、下划线', true);
  }
  $('buildResult').innerHTML = '<div class="res warn">正在提交…</div>';
  $('btnBuild').disabled = true;
  try {
    const j = await api('/api/dataset', {
      session: S.session, name,
      pack_block_size: +$('packSize').value,
      val_ratio: +$('valRatio').value,
      test_ratio: +$('testRatio').value,
    });
    pollJob(j.id, renderBuild, 'buildResult');
    toast('正在打包（首次会下载分词器，请耐心等待）');
  } catch (e) {
    showErr('buildResult', e);
  } finally {
    $('btnBuild').disabled = false;
  }
};

function renderBuild(job) {
  const r = job.result;
  const sp = r.splits || {};
  const splitTxt = Object.entries(sp).map(([k, v]) => `${k} ${v}`).join(' · ');
  $('buildResult').innerHTML = `
    <div class="res ok"><h4>数据集已生成 ✓</h4>
      <div class="stats">
        <div class="stat"><div class="v">${(r.n_samples || 0).toLocaleString()}</div><div class="k">样本</div></div>
        <div class="stat"><div class="v">${(r.n_tokens || 0).toLocaleString()}</div><div class="k">token</div></div>
        ${r.n_blocks ? `<div class="stat"><div class="v">${r.n_blocks.toLocaleString()}</div><div class="k">定长块</div></div>` : ''}
        <div class="stat"><div class="v">${r.n_shards}</div><div class="k">分片文件</div></div>
      </div>
      <p class="meta">切分：${escapeHtml(splitTxt)}</p>
      <p class="meta">${r.n_truncated ? `有 ${r.n_truncated} 条超过长度上限被截断。` : '没有样本被截断。'}
        ${r.leakage_clean === false ? '<b style="color:var(--err)">检测到训练/测试之间有重复样本，请检查。</b>' : '已检查：训练集与测试集之间无重复。'}</p>
      <p class="meta" style="margin-top:10px">产物目录：<code>${escapeHtml(r.dataset_root)}</code></p>
      <p class="meta">怎么用：<code>load_dataset("parquet", data_files="…/train-*.parquet")</code></p>
    </div>`;
}

/* ── 导航 ── */
$('backTo1').onclick = () => goto(1);
$('backTo2').onclick = () => goto(2);
$('backTo3').onclick = () => goto(3);

loadScenarios();