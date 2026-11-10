/**
 * Приёмщик для бота дневника питания.
 *
 * Раз в минуту забирает новые сообщения у Telegram, складывает их в лист «Входящие»
 * и запускает GitHub Actions — бот разбирает запись и отвечает примерно через минуту.
 * Если приёмщик сломается, бот сам вернётся к старому режиму (по расписанию).
 *
 * Установка: Таблица → Расширения → Apps Script → вставить этот код → заполнить 2 строки ниже →
 * выбрать функцию setup → Выполнить → разрешить доступ. Подробно — в README.
 */
const TELEGRAM_TOKEN = 'ВСТАВЬ_ТОКЕН_БОТА';
const GITHUB_TOKEN = 'ВСТАВЬ_ТОКЕН_GITHUB';
const GITHUB_REPO = 'Airatik2004/food-bot';
const WORKFLOW_FILE = 'bot.yml';

const INBOX = 'Входящие';
const STATE = 'Служебное';

/** Один раз запустить вручную: включает приём сообщений. */
function setup() {
  // getUpdates не работает, пока у бота стоит webhook
  tg_('deleteWebhook', { drop_pending_updates: false });

  // начать с того места, где остановился бот на GitHub
  const props = PropertiesService.getScriptProperties();
  if (!props.getProperty('OFFSET')) {
    const last = Number(stateValue_('last_update_id') || 0);
    props.setProperty('OFFSET', String(last));
  }

  // запуск poll раз в минуту (без дублей)
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'poll')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('poll').timeBased().everyMinutes(1).create();

  // проверка токена GitHub
  const code = dispatch_();
  Logger.log(code === 204
    ? 'Готово! GitHub отвечает. Напиши боту что-нибудь — ответ придёт примерно через минуту.'
    : 'Telegram подключён, но GitHub ответил кодом ' + code + '. Проверь GITHUB_TOKEN и права Actions: Read and write.');
  poll();
}

/** Вызывается триггером раз в минуту. */
function poll() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    const props = PropertiesService.getScriptProperties();
    const offset = Number(props.getProperty('OFFSET') || 0);
    const res = tg_('getUpdates', {
      offset: offset + 1,
      timeout: 0,
      allowed_updates: JSON.stringify(['message', 'edited_message', 'callback_query'])
    });
    setState_('receiver', new Date().toISOString());
    if (!res.ok || !res.result.length) return;

    const sh = SpreadsheetApp.getActive().getSheetByName(INBOX)
      || SpreadsheetApp.getActive().insertSheet(INBOX);
    if (sh.getLastRow() === 0) sh.appendRow(['update_id', 'Получено', 'JSON']);

    let last = offset;
    const rows = [];
    res.result.forEach(u => {
      rows.push([u.update_id, new Date(), JSON.stringify(u)]);
      last = Math.max(last, u.update_id);
      if (u.callback_query) {  // убрать «часики» на кнопке сразу
        tg_('answerCallbackQuery', { callback_query_id: u.callback_query.id, text: 'Принял, считаю…' });
      }
    });
    sh.getRange(sh.getLastRow() + 1, 1, rows.length, 3).setValues(rows);
    props.setProperty('OFFSET', String(last));
    dispatch_();
  } finally {
    lock.releaseLock();
  }
}

/** Отключить приёмщик (бот вернётся к работе по расписанию). */
function stop() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  setState_('receiver', '');
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
  const r = UrlFetchApp.fetch('https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/' + method, {
    method: 'post', payload: params, muteHttpExceptions: true
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
