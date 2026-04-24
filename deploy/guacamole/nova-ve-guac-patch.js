/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

(function () {
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

  function installPatchLoop() {
    suppressKnownClientNoise();
    stripDeprecatedViewportKey();

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
