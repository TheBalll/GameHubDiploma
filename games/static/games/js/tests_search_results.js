const test = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {Script} = require("node:vm");

const script = new Script(readFileSync(join(__dirname, "search-results.js"), "utf8"));

class Element {
    constructor(tag) {
        this.tag = tag;
        this.children = [];
        this.events = {};
        this.style = {};
        this.value = "";
    }

    appendChild(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
    addEventListener(name, callback) { this.events[name] = callback; }
    setAttribute() {}
    insertAdjacentElement() {}
}

async function loadSearch(canImport, game, importStatus = 200) {
    const elements = {};
    for (const suffix of ["app", "query", "genre", "sort", "grid", "count", "form"]) {
        elements[`search-page-${suffix}`] = new Element("div");
    }
    elements["search-page-app"].dataset = {apiUrl: "/search/", loginUrl: "/login/", canImport: String(canImport)};
    const location = {href: "https://example.test/search/", search: ""};
    const calls = [];
    script.runInNewContext({
        document: {
            getElementById: id => elements[id],
            createElement: tag => new Element(tag),
            createDocumentFragment: () => new Element("fragment"),
            querySelector: selector => {
                assert.equal(selector, "#game-import-csrf-form input[name='csrfmiddlewaretoken']");
                return {value: "fixture-csrf"};
            },
            addEventListener: (name, callback) => callback(),
        },
        window: {location, history: {replaceState() {}}},
        URL, URLSearchParams, AbortController,
        IntersectionObserver: class { observe() {} },
        fetch: async (url, options) => {
            calls.push({url, options});
            if (url.startsWith("/search/?")) {
                return {ok: true, json: async () => ({games: [game], genres: [], page: 1, has_next: false, count: 1})};
            }
            return {ok: importStatus === 200, status: importStatus, json: async () => ({success: true, url: "/games/42/"})};
        },
    });
    await new Promise(resolve => setImmediate(resolve));
    const [fragment] = elements["search-page-grid"].children;
    return {action: fragment.children[0].children[1].children.at(-1), calls, location};
}

test("guest search opens an external provider or sign-in without an import request", async () => {
    const link = "https://www.igdb.com/games/fixture";
    const [known, missing] = await Promise.all([
        loadSearch(false, {source: "igdb", id: 123, title: "Fixture", store_url: link}),
        loadSearch(false, {source: "igdb", id: 124, title: "Fixture"}),
    ]);
    assert.equal(known.action.tag, "a");
    assert.equal(known.action.href, link);
    assert.equal(missing.action.href, "/login/");
    assert.equal(missing.action.textContent, "Sign In");
    assert.equal(known.calls.length, 1);
    assert.equal(missing.calls.length, 1);
});

test("signed-in search imports with POST and CSRF, then opens the local game", async () => {
    const {action, calls, location} = await loadSearch(true, {source: "steam", id: 456, title: "Fixture"});
    assert.equal(action.tag, "button");
    assert.equal(calls.length, 1);
    await action.events.click();
    assert.equal(calls[1].url, "/games/import-steam/456/");
    assert.equal(calls[1].options.method, "POST");
    assert.equal(calls[1].options.headers["X-CSRFToken"], "fixture-csrf");
    assert.equal(location.href, "/games/42/");
});

test("search import sends an expired session to sign-in", async () => {
    const {action, calls, location} = await loadSearch(true, {source: "igdb", id: 123, title: "Fixture"}, 401);
    await action.events.click();
    assert.equal(calls[1].url, "/games/import-igdb/123/");
    assert.equal(location.href, "/login/");
});
