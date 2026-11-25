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
