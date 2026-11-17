/**
 * Итог дня ровно в 5 утра: GitHub-расписание часто опаздывает на часы,
 * поэтому будильник ставит Google — каждый день примерно в 05:01–05:30 по Москве он запускает бота.
 * Этот файл лежит рядом с Code.gs в том же проекте (токены берутся оттуда).
 * После вставки один раз запусти setupDaily.
 */
function setupDaily() {
  ScriptApp.getProjectTriggers()
    .filter(t => t.getHandlerFunction() === 'daily')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('daily').timeBased()
    .everyDays(1).atHour(5).nearMinute(16).inTimezone('Europe/Moscow').create();
  Logger.log('Готово: итог за день будет приходить около 05:01–05:30. GitHub ответил: ' + dispatch_());
}

function daily() {
  dispatch_();
}
