/* The token is kept in a variable for the life of the page and nothing else.
   It is deliberately not put in localStorage or sessionStorage: it grants access to unpublished
   work, and a shared or borrowed browser should not retain it after the tab closes. */
(function () {
  "use strict";

  var token = null;
  var el = function (id) { return document.getElementById(id); };

  function show(node, html) { node.innerHTML = html; node.hidden = false; }
  function hide(node) { node.hidden = true; node.innerHTML = ""; }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function cookie(name) {
    var hit = document.cookie.split("; ").find(function (row) {
      return row.indexOf(name + "=") === 0;
    });
    return hit ? decodeURIComponent(hit.slice(name.length + 1)) : null;
  }

  function request(path) {
    var headers = {};
    if (token) { headers.Authorization = "Review " + token; }
    return fetch(path, { headers: headers }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var detail = body && body.detail ? body.detail : "Request failed";
          throw new Error(detail + " (" + response.status + ")");
        }
        return body;
      });
    });
  }

  function renderEntry(entry) {
    var embargo = entry.embargoed ? ' <span class="tag">embargoed</span>' : "";
    var rows = [
      ["Accession", escapeHtml(entry.accession) + embargo],
      ["Title", escapeHtml(entry.title)],
      ["Method", escapeHtml(entry.method)],
      ["Resolution", entry.resolution == null ? "not applicable" : escapeHtml(entry.resolution) + " &#8491;"],
      ["Depositor", escapeHtml(entry.depositor && entry.depositor.name)],
      ["ORCID", entry.depositor && entry.depositor.orcid
        ? '<a href="https://orcid.org/' + escapeHtml(entry.depositor.orcid) + '" rel="noreferrer noopener">'
          + escapeHtml(entry.depositor.orcid) + "</a>"
        : "not recorded"],
      ["Deposited", escapeHtml(entry.deposited_on)],
      ["Releases", escapeHtml(entry.release_on)]
    ];
    var html = "<dl>" + rows.map(function (row) {
      return "<dt>" + row[0] + "</dt><dd>" + row[1] + "</dd>";
    }).join("") + "</dl>";
    if (entry.coordinates_url) {
      html += '<p><button type="button" id="download">Download coordinates</button></p>';
    }
    show(el("entry"), html);
    el("entry-empty").hidden = true;
    var button = el("download");
    if (button) { button.addEventListener("click", function () { download(entry); }); }
  }

  function download(entry) {
    var headers = {};
    if (token) { headers.Authorization = "Review " + token; }
    fetch(entry.coordinates_url, { headers: headers })
      .then(function (response) {
        if (!response.ok) { throw new Error("Download refused (" + response.status + ")"); }
        return response.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var link = document.createElement("a");
        link.href = url;
        link.download = entry.accession + ".cif";
        link.click();
        URL.revokeObjectURL(url);
      })
      .catch(function (err) { show(el("error"), escapeHtml(err.message)); });
  }

  el("token-form").addEventListener("submit", function (event) {
    event.preventDefault();
    hide(el("error"));
    hide(el("session"));
    token = el("token").value.trim();
    if (!token) { return; }
    request("/api/session/")
      .then(function (session) {
        var remaining = session.accesses_remaining == null
          ? "no limit"
          : session.accesses_remaining + " remaining";
        show(el("session"),
          "<dl><dt>Entry</dt><dd><code>" + escapeHtml(session.accession) + "</code></dd>"
          + "<dt>Issued for</dt><dd>" + escapeHtml(session.issued_for) + "</dd>"
          + "<dt>Expires</dt><dd>" + escapeHtml(new Date(session.expires_at).toUTCString()) + "</dd>"
          + "<dt>Accesses</dt><dd>" + escapeHtml(remaining) + "</dd></dl>");
        return request("/api/depositions/" + encodeURIComponent(session.accession) + "/");
      })
      .then(renderEntry)
      .catch(function (err) {
        token = null;
        show(el("error"), escapeHtml(err.message));
      });
  });

  // Depositor sign-in. Whether anyone is signed in is not rendered into the page: it is
  // discovered by asking the API, so the template stays a static file and there is one place
  // that decides who may see what.
  var SIGNIN_MESSAGES = {
    ok: "Signed in.",
    failed: "That sign-in could not be completed. Please try again.",
    unknown: "That ORCID iD has no depositions here.",
    unavailable: "ORCID sign-in is not configured on this server.",
    out: "Signed out."
  };

  function showSigninNote() {
    var value = new URLSearchParams(window.location.search).get("signin");
    if (value && SIGNIN_MESSAGES[value]) {
      show(el("signin-note"), escapeHtml(SIGNIN_MESSAGES[value]));
    }
  }

  function loadMine() {
    fetch("/api/mine/")
      .then(function (response) {
        if (response.status === 401) { return null; }
        if (!response.ok) { throw new Error("Could not load your depositions"); }
        return response.json();
      })
      .then(function (entries) {
        if (entries === null) { return; }
        el("signin-actions").innerHTML =
          '<a class="button-link secondary" href="/auth/orcid/logout/">Sign out</a>';
        el("deposit-form").hidden = false;
        var rows = entries.results || entries;
        el("mine-list").innerHTML = rows.length
          ? rows.map(function (entry) {
              var tag = entry.embargoed
                ? ' <span class="tag">embargoed</span>'
                  + ' <button type="button" class="row-action" data-release="'
                  + escapeHtml(entry.accession) + '">Release now</button>'
                : "";
              return "<li><code>" + escapeHtml(entry.accession) + "</code> &mdash; "
                + escapeHtml(entry.title) + tag + "</li>";
            }).join("")
          : "<li>No depositions.</li>";
        Array.prototype.forEach.call(
          document.querySelectorAll("[data-release]"), function (button) {
            button.addEventListener("click", function () {
              releaseEntry(button.getAttribute("data-release"));
            });
          });
      })
      .catch(function (err) { show(el("signin-note"), escapeHtml(err.message)); });
  }

  function releaseEntry(accession) {
    if (!window.confirm("Release " + accession + " now? This cannot be undone, and the entry "
        + "becomes public immediately.")) { return; }
    fetch("/api/depositions/" + encodeURIComponent(accession) + "/release/", {
      method: "POST",
      headers: { "X-CSRFToken": cookie("csrftoken") || "" }
    })
      .then(function (response) {
        if (!response.ok) { throw new Error("Release refused (" + response.status + ")"); }
        show(el("deposit-note"), escapeHtml(accession + " is now public."));
        loadMine();
      })
      .catch(function (err) { show(el("deposit-note"), escapeHtml(err.message)); });
  }

  el("deposit-form").addEventListener("submit", function (event) {
    event.preventDefault();
    hide(el("deposit-note"));
    var body = new FormData();
    body.append("coordinates", el("coordinates").files[0]);
    body.append("release_on", el("release_on").value);
    fetch("/api/deposit/", {
      method: "POST",
      headers: { "X-CSRFToken": cookie("csrftoken") || "" },
      body: body
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok) {
            var detail = payload.coordinates || payload.release_on || payload.detail
              || "Deposit refused";
            throw new Error([].concat(detail).join(" "));
          }
          return payload;
        });
      })
      .then(function (entry) {
        show(el("deposit-note"),
          escapeHtml(entry.accession + " deposited, embargoed until " + entry.release_on + "."));
        el("deposit-form").reset();
        loadMine();
      })
      .catch(function (err) { show(el("deposit-note"), escapeHtml(err.message)); });
  });

  showSigninNote();
  loadMine();

  // The public list is fetched without a token, which is the point of it.
  fetch("/api/depositions/")
    .then(function (r) { return r.json(); })
    .then(function (entries) {
      var list = el("public-list");
      var rows = entries.results || entries;
      if (!rows.length) { list.innerHTML = "<li>None loaded yet.</li>"; return; }
      list.innerHTML = rows.map(function (entry) {
        return "<li><code>" + escapeHtml(entry.accession) + "</code> &mdash; "
          + escapeHtml(entry.title) + "</li>";
      }).join("");
    })
    .catch(function () { el("public-list").innerHTML = "<li>Could not load.</li>"; });
}());
