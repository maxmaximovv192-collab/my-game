/* Shared rating for all built-in question modes. Local game progress stays separate. */
var Rating = (function(){
  var queue = [], key = null, running = false, retryTimer = null, expired = false;
  var generation = 0, storageFailed = false;
  function telegram(){ return window.Telegram && window.Telegram.WebApp; }
  function base(){ return (window.RATING_API_URL || "").replace(/\/+$/, ""); }
  function available(){ var tg = telegram(); return !!(base() && tg && tg.initData); }
  function store(){
    try { localStorage.setItem(key, JSON.stringify(queue)); storageFailed = false; }
    catch(e){ storageFailed = true; }
  }
  function init(){
    var tg = telegram(), id = tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id;
    if (!id || !available()) return;
    key = "duty-rating-queue-v1-" + id;
    try { queue = JSON.parse(localStorage.getItem(key) || "[]"); if (!Array.isArray(queue)) queue = []; }
    catch(e){ queue = []; }
    flush();
    window.addEventListener("online", flush);
    document.addEventListener("visibilitychange", function(){ if (!document.hidden) flush(); });
  }
  async function request(path, data){
    var controller = new AbortController(), timeout = setTimeout(function(){ controller.abort(); }, 10000);
    try {
      var response = await fetch(base() + path, {
        method: data ? "POST" : "GET",
        headers: { "Authorization": "tma " + telegram().initData, "Content-Type": "application/json" },
        body: data ? JSON.stringify(data) : undefined, signal: controller.signal
      });
      if (!response.ok){ var error = new Error("HTTP " + response.status); error.status = response.status; throw error; }
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  function record(question, selected){
    if (!key) return;
    var index = BASE.indexOf(question);
    if (index < 0) return;
    var entry = { event_id: crypto.randomUUID(), question: index, answer: selected, text: question.q };
    queue.push(entry); store();
    flush();
  }
  async function flush(){
    if (running || expired || !available() || !queue.length) return;
    running = true; clearTimeout(retryTimer);
    try {
      while (queue.length){
        try { await request("/api/answer", queue[0]); }
        catch(e){
          if (e.status === 400){ queue.shift(); store(); continue; }
          if (e.status === 401) expired = true;
          throw e;
        }
        queue.shift(); store();
      }
    } catch(e){
      if (!expired) retryTimer = setTimeout(flush, 15000);
    } finally { running = false; }
  }
  function status(){
    if (!base()) return "Общий рейтинг ещё не подключён. Игровой прогресс сохраняется как обычно.";
    if (!available()) return "Откройте приложение кнопкой /play в Telegram, чтобы участвовать в рейтинге.";
    if (expired) return "Сессия Telegram истекла. Закройте и заново откройте приложение из бота.";
    if (storageFailed) return "Не удалось сохранить очередь ответов. Не закрывайте приложение до синхронизации.";
    if (queue.length) return "Ожидают отправки: " + queue.length + ". Баллы появятся после синхронизации.";
    return "Игровые события синхронизированы с ботом.";
  }
  async function screen(){
    var ownGeneration = ++generation;
    shell('<div class="panel"><p class="eyebrow">Общий рейтинг</p>' +
      '<p>Здесь появятся лидеры и ваше место, когда будут настроены правила рейтинга.</p>' +
      '<p class="note">Сейчас подготовлена связь с ботом. Начисление баллов и ежедневная рассылка пока выключены.</p>' +
      '<p class="note" id="rating-status" role="status">'+esc(status())+'</p>' +
      '<div id="rating-result" aria-live="polite"></div><button class="btn ghost" id="rating-refresh">Обновить</button></div>');
    document.getElementById("rating-refresh").onclick = screen;
    if (!available() || expired) return;
    await flush();
    try {
      var data = await request("/api/rating");
      if (view !== "rating" || ownGeneration !== generation) return;
      var result = document.getElementById("rating-result"), me = data.me;
      if (!data.configured){
        result.innerHTML = '<p>Правила рейтинга пока не настроены. Игровые события сохраняются без начисления баллов.</p>';
        document.getElementById("rating-status").textContent = status();
        return;
      }
      result.innerHTML = (me ? '<div class="panel"><b>Ваше место: '+me.place+' из '+data.total+'</b><p>'+me.points+' балл.</p></div>' : '') +
        (data.top.length ? data.top.map(function(row){ return '<div class="urow"><span>'+row.place+'. '+esc(row.name)+'</span><b>'+row.points+' балл.</b></div>'; }).join("") : '<p>Пока нет верных ответов. Станьте первым!</p>');
      document.getElementById("rating-status").textContent = status();
    } catch(e){
      if (e.status === 401) expired = true;
      if (view === "rating" && ownGeneration === generation){
        document.getElementById("rating-status").textContent = expired ? status() : "Сервер рейтинга недоступен. Ответы сохраняются на устройстве и будут отправлены при восстановлении связи.";
      }
    }
  }
  return { init: init, record: record, screen: screen };
})();
