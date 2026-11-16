/**
 * Приёмщик для бота дневника питания.
 * Telegram → этот скрипт → лист «Входящие» → запуск бота на GitHub. Пока бот думает — «печатает…».
 * Сообщения приходят мгновенно (вебхук), а если вебхук сбоит — скрипт сам переходит на опрос раз в минуту.
 */
const TELEGRAM_TOKEN = 'ВСТАВЬ_ТОКЕН_БОТА';
const GITHUB_TOKEN = 'ВСТАВЬ_ТОКЕН_GITHUB';
const GITHUB_REPO = 'Airatik2004/food-bot';
const WORKFLOW_FILE = 'bot.yml';
const WEBAPP_URL = '';      // можно оставить пустым; если setup попросит — вставь сюда URL веб-приложения
const TYPING_SECONDS = 45;  // сколько максимум показывать «печатает…», пока бот запускается

const INBOX = 'Входящие';
const STATE = 'Служебное';
const ALLOWED = JSON.stringify(['message', 'edited_message', 'callback_query']);

/** Запустить вручную после вставки кода и после каждого нового развертывания. */
function setup() {
  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty('OFFSET')) {
    props.setProperty('OFFSET', String(Number(stateValue_('last_update_id') || 0)));
  }
  if (!props.getProperty('HOOK_KEY')) {
    props.setProperty('HOOK_KEY', Utilities.getUuid().replace(/-/g, ''));
  }
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('poll').timeBased().everyMinutes(1).create();

  const gh = dispatch_();
  const ghMsg = gh === 204 ? 'GitHub: ок.' : 'GitHub ответил кодом ' + gh + ' — проверь GITHUB_TOKEN.';

  // сначала забрать то, что накопилось, потом включить вебхук
  tg_('deleteWebhook', { drop_pending_updates: false });
  pollUpdates_();
  const url = WEBAPP_URL || ScriptApp.getService().getUrl() || '';
  let hookMsg;
  if (/\/exec$/.test(url)) {
    const r = tg_('setWebhook', {
      url: url + '?k=' + props.getProperty('HOOK_KEY'), allowed_updates: ALLOWED, max_connections: 10
    });
    hookMsg = r.ok ? 'Вебхук включён: сообщения доходят сразу.'
      : 'Вебхук не включился (' + r.description + '), работаю опросом раз в минуту.';
    setState_('webhook', r.ok ? 'on' : 'off');
  } else {
    hookMsg = 'Веб-приложение не найдено — работаю опросом раз в минуту. '
      + 'Чтобы ускорить: Развернуть → Новое развертывание → Веб-приложение, затем снова setup.';
    setState_('webhook', 'off');
  }
  Logger.log('Готово! ' + ghMsg + ' ' + hookMsg);
}

/** Telegram присылает сюда каждое сообщение сразу (вебхук). */
function doPost(e) {
  try {
    const key = PropertiesService.getScriptProperties().getProperty('HOOK_KEY');
    if (!e || !e.parameter || e.parameter.k !== key || !e.postData) return ok_();
    const u = JSON.parse(e.postData.contents);
    const fresh = accept_([u]);
    if (fresh.length) keepTyping_(fresh);
  } catch (err) {
    console.error(err);
  }
  return ok_();
}

/** Раз в минуту: отметиться, проверить вебхук; если он сбоит — забрать сообщения опросом. */
function poll() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    setState_('receiver', new Date().toISOString());
    const info = (tg_('getWebhookInfo', {}).result) || {};
    if (info.url) {
      const now = Date.now() / 1000;
      const stuck = info.pending_update_count > 0 && info.last_error_date && now - info.last_error_date < 300;
      if (!stuck) return;
      // вебхук сбоит (например, 302 от Google) — выключаем и работаем опросом
      tg_('deleteWebhook', { drop_pending_updates: false });
      setState_('webhook', 'off: ' + (info.last_error_message || 'ошибка'));
    }
    const fresh = pollUpdates_();
    if (fresh.length) keepTyping_(fresh);
  } finally {
    lock.releaseLock();
  }
}

/** Отключить приёмщик (бот вернётся к работе по расписанию). */
function stop() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  tg_('deleteWebhook', { drop_pending_updates: false });
  setState_('receiver', '');
  setState_('webhook', 'off');
}

function pollUpdates_() {
  const props = PropertiesService.getScriptProperties();
  const offset = Number(props.getProperty('OFFSET') || 0);
  const res = tg_('getUpdates', { offset: offset + 1, timeout: 0, allowed_updates: ALLOWED });
  if (!res.ok || !res.result.length) return [];
  const fresh = accept_(res.result);
  const last = res.result.reduce((m, u) => Math.max(m, u.update_id), offset);
  props.setProperty('OFFSET', String(last));
  return fresh;
}

/** Сохранить новые апдейты, снять «часики» с кнопок, запустить бота. Возвращает новые. */
function accept_(updates) {
  const cache = CacheService.getScriptCache();
  const fresh = updates.filter(u => {
    const k = 'u' + u.update_id;
    if (cache.get(k)) return false;   // Telegram мог прислать повтор
    cache.put(k, '1', 21600);
    return true;
  });
  if (!fresh.length) return [];
  const ss = SpreadsheetApp.getActive();
  const sh = ss.getSheetByName(INBOX) || ss.insertSheet(INBOX);
  if (sh.getLastRow() === 0) sh.appendRow(['update_id', 'Получено', 'JSON']);
  fresh.forEach(u => {
    sh.appendRow([u.update_id, new Date(), JSON.stringify(u)]);
    if (u.callback_query) {
      tg_('answerCallbackQuery', { callback_query_id: u.callback_query.id, text: 'Принял, считаю…' });
    }
  });
  SpreadsheetApp.flush();
  dispatch_();
  return fresh;
}

/** «Печатает…», пока бот на GitHub не обработает сообщения (или не выйдет время). */
function keepTyping_(updates) {
  const chats = {};
  let maxId = 0;
  updates.forEach(u => {
    const m = u.message || u.edited_message || (u.callback_query && u.callback_query.message);
    if (m && m.chat) chats[m.chat.id] = true;
    maxId = Math.max(maxId, u.update_id);
  });
  const end = Date.now() + TYPING_SECONDS * 1000;
  while (Date.now() < end) {
    if (Number(stateValue_('last_update_id') || 0) >= maxId) return;
    Object.keys(chats).forEach(c => tg_('sendChatAction', { chat_id: c, action: 'typing' }));
    Utilities.sleep(4000);
  }
}

function ok_() {
  return HtmlService.createHtmlOutput('ok');
}

function dispatch_() {
  const r = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + GITHUB_REPO + '/actions/workflows/' + WORKFLOW_FILE + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + GITHUB_TOKEN, Accept: 'application/vnd.github+json' },
      payload: JSON.stringify({ ref: 'main' }),
      muteHttpExceptions: true
    });
  return r.getResponseCode();
}

function tg_(method, params) {
  // Все значения — строками: числа в payload UrlFetchApp может не передать,
  // и тогда Telegram не получает offset и присылает одни и те же сообщения снова.
  const payload = {};
  Object.keys(params || {}).forEach(k => { payload[k] = String(params[k]); });
  const r = UrlFetchApp.fetch('https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/' + method, {
    method: 'post', payload: payload, muteHttpExceptions: true
  });
  return JSON.parse(r.getContentText());
}

function stateRow_(key) {
  const sh = SpreadsheetApp.getActive().getSheetByName(STATE);
  const keys = sh.getRange(1, 1, Math.max(sh.getLastRow(), 1), 1).getValues().map(r => String(r[0]));
  return { sh: sh, row: keys.indexOf(key) + 1 };
}

function stateValue_(key) {
  const s = stateRow_(key);
  return s.row ? s.sh.getRange(s.row, 2).getValue() : '';
}

function setState_(key, value) {
  const s = stateRow_(key);
  const row = s.row || s.sh.getLastRow() + 1;
  s.sh.getRange(row, 1, 1, 2).setValues([[key, "'" + value]]);
}
