/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

(function () {
  /* Terminal font families shipped by nova-ve (deploy/guacamole/fonts,
   * installed into guacd). Offered as suggestions on the client menu's
   * "Font name" field so operators get a dropdown instead of guessing. */
  var NOVA_VE_TERMINAL_FONTS = [
    'Roboto Mono',
    'JetBrainsMono Nerd Font Mono',
    'FiraCode Nerd Font Mono',
    'MesloLGS Nerd Font Mono'
  ];

  function suppressKnownClientNoise() {
    var knownMessage = "Cannot read properties of undefined (reading 'bind')";
    if (window.__novaVeConsolePatched) return;

    var originalConsoleError = window.console.error.bind(window.console);
    window.console.error = function () {
      if (arguments.length && String(arguments[0]).includes(knownMessage)) return;
      return originalConsoleError.apply(window.console, arguments);
    };

    window.addEventListener(
      'error',
      function (event) {
        if (event && typeof event.message === 'string' && event.message.includes(knownMessage)) {
          event.preventDefault();
        }
      },
      true
    );

    window.__novaVeConsolePatched = true;
  }

  function stripDeprecatedViewportKey() {
    var viewportMeta = document.querySelector('meta[name="viewport"]');
    if (!viewportMeta || !viewportMeta.content) return;
    viewportMeta.content = viewportMeta.content.replace(/,?target-densitydpi=[^,]+/, '');
  }

  function patchTunnelService() {
    if (typeof angular === 'undefined') return false;

    var injector = angular.element(document.body).injector();
    if (!injector) return false;

    var $q = injector.get('$q');
    var tunnelService = injector.get('tunnelService');
    if (!tunnelService || tunnelService.__novaVePatched) return !!tunnelService;

    var originalGetSharingProfiles = tunnelService.getSharingProfiles
      ? tunnelService.getSharingProfiles.bind(tunnelService)
      : null;
    if (originalGetSharingProfiles) {
      tunnelService.getSharingProfiles = function (uuid) {
        return originalGetSharingProfiles(uuid).catch(function () {
          return $q.when([]);
        });
      };
    }

    var originalGetProtocol = tunnelService.getProtocol
      ? tunnelService.getProtocol.bind(tunnelService)
      : null;
    if (originalGetProtocol) {
      tunnelService.getProtocol = function (uuid) {
        return originalGetProtocol(uuid).catch(function () {
          return $q.when('');
        });
      };
    }

    tunnelService.__novaVePatched = true;
    return true;
  }

  function installFontNameSuggestions() {
    var datalist = document.getElementById('nova-ve-terminal-fonts');
    if (!datalist) {
      datalist = document.createElement('datalist');
      datalist.id = 'nova-ve-terminal-fonts';
      NOVA_VE_TERMINAL_FONTS.forEach(function (name) {
        var option = document.createElement('option');
        option.value = name;
        datalist.appendChild(option);
      });
      document.body.appendChild(datalist);
    }

    var inputs = document.querySelectorAll('.labeled-field-font-name input[type=text]');
    for (var i = 0; i < inputs.length; i += 1) {
      if (inputs[i].getAttribute('list')) continue;
      inputs[i].setAttribute('list', 'nova-ve-terminal-fonts');
      inputs[i].setAttribute('autocomplete', 'off');
    }
  }

  function startFontSuggestionWatch() {
    // The client menu renders/re-renders after connect; keep attaching
    // the font suggestions as the menu DOM appears. Idempotent guard so
    // the retry loop below can never double-install it.
    if (window.__novaVeFontWatch) return;
    window.__novaVeFontWatch = window.setInterval(installFontNameSuggestions, 2000);
  }

  function installPatchLoop() {
    suppressKnownClientNoise();
    stripDeprecatedViewportKey();
    installFontNameSuggestions();
    startFontSuggestionWatch();

    if (patchTunnelService()) return;

    var attempts = 0;
    var interval = window.setInterval(function () {
      stripDeprecatedViewportKey();
      attempts += 1;
      if (patchTunnelService() || attempts >= 100) {
        window.clearInterval(interval);
      }
    }, 200);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installPatchLoop, { once: true });
  } else {
    installPatchLoop();
  }
})();
