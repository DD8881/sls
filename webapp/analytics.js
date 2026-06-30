// analytics.js — PostHog instrumentation for the Mini App.
//
// Loaded BEFORE app.js (see index.html), so window.track() exists by the time
// app.js wires up its handlers. Everything here is defensive: if the key is
// blank or posthog fails to load (Telegram webviews vary a lot), track() stays a
// no-op and the app is unaffected. Analytics must never break the product.
//
// Identity: distinct_id is namespaced `sls:<telegram_id>` so a second bot added
// to the same PostHog project never collides on the same Telegram user id, and
// so the user's bot-side events (worker/index.js) and app-side events unify into
// one person. `source: 'miniapp'` is registered on every event to separate
// client events from the bot's server-side ones.

(function () {
  // Public, ingest-only project key — safe to ship in client code. Blank it to
  // disable analytics entirely (track() degrades to a no-op).
  var KEY = 'phc_t9kG4uNFjDmQuVnUQNPjZrnxqGHMyoMeRdcaN2eFdscn';
  var HOST = 'https://us.i.posthog.com';

  window.track = function () {};            // safe default until init succeeds
  if (!KEY) return;

  // --- Official PostHog loader snippet: stubs `posthog`, queues any early
  //     calls, and async-loads array.js (assets host derived from api_host). ---
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister unregister_for_session getFeatureFlag getFeatureFlagPayload isFeatureEnabled reloadFeatureFlags updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures on onFeatureFlags onSessionId getSurveys getActiveMatchingSurveys renderSurvey canRenderSurvey getNextSurveyStep identify setPersonProperties group resetGroups setPersonPropertiesForFlags resetPersonPropertiesForFlags setGroupPropertiesForFlags resetGroupPropertiesForFlags reset get_distinct_id getGroups get_session_id get_session_replay_url alias set_config startSessionRecording stopSessionRecording sessionRecordingStarted captureException loadToolbar get_property getSessionProperty createPersonProfile opt_in_capturing opt_out_capturing has_opted_in_capturing has_opted_out_capturing clear_opt_in_out_capturing debug getPageViewId captureTraceFeedback captureTraceMetric".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);

  posthog.init(KEY, {
    api_host: HOST,
    autocapture: false,       // named events only — protects the free quota and keeps data clean
    capture_pageview: false,  // single-page shell; app_opened is our entry event
    capture_pageleave: false,
    // Session Replay is controlled REMOTELY by the PostHog project toggle
    // (Settings → Replay) — not hardcoded here — so it flips on/off with zero
    // redeploy. We just don't block it at the SDK level (false = defer to the
    // remote config) and pre-arm input masking so a flip is privacy-safe at once.
    disable_session_recording: false,
    session_recording: { maskAllInputs: true },  // mask feedback/search text in the recording (events still capture the query)
    persistence: 'localStorage',
    person_profiles: 'identified_only',  // anonymous (non-Telegram) opens don't create person rows
  });
  posthog.register({ source: 'miniapp' });  // super-property on every client event

  // Identify the Telegram user up front so every later event attaches to them.
  try {
    var tgw = window.Telegram && window.Telegram.WebApp;
    var u = tgw && tgw.initDataUnsafe ? tgw.initDataUnsafe.user : null;
    if (u && u.id) {
      posthog.identify('sls:' + u.id, {
        telegram_username: u.username || null,
        language_code: u.language_code || null,
        is_premium: !!u.is_premium,
      });
    }
  } catch (e) { /* no initData (e.g. opened outside Telegram) — stay anonymous */ }

  window.track = function (event, props) {
    try { posthog.capture(event, props || {}); } catch (e) { /* never throw into the app */ }
  };
})();
