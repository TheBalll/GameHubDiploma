const test = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {Script} = require("node:vm");

const code = new Script(readFileSync(join(__dirname, "recommendations.js"), "utf8"));
const markup = readFileSync(join(__dirname, "../../../templates/games/_recommendations.html"), "utf8");

class Element {
    constructor(hidden = false) {
        this.hidden = hidden;
        this.children = [];
        this.events = {};
        this.attributes = {};
        this.textContent = "";
    }

    addEventListener(name, handler) { this.events[name] = handler; }
    fire(name) { return this.events[name]?.(); }
    setAttribute(name, value) { this.attributes[name] = value; }
    focus() { this.focused = true; }
    querySelector(selector) { return this.nodes[selector]; }

    replaceChildren(...children) {
        this.children = children;
        children.forEach(child => { child.parent = this; });
    }

    remove() {
        const siblings = this.parent.children;
        siblings.splice(siblings.indexOf(this), 1);
    }
}

const nextTick = () => new Promise(resolve => setImmediate(resolve));
const response = (payload, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => payload});
const localGame = (overrides = {}) => ({
    title: "Catalog Game", cover: null, genres: [], release_year: null, match_score: null, reason: null,
    game_url: "/games/1/", add_url: "/games/1/add-to-library/", add_token: null, ...overrides,
});

function makeCard() {
    const card = new Element();
    card.nodes = {};
    for (const name of ["link", "title", "cover", "placeholder", "meta", "match", "reason", "add", "error"]) {
        assert.match(markup, new RegExp(`data-card-${name}(?![-\\w])`));
        card.nodes[`[data-card-${name}]`] = new Element(!["title", "placeholder", "add"].includes(name));
    }
    return card;
}

function setup(...replies) {
    const section = new Element();
    section.dataset = {url: "/recommendations/data/"};
    section.nodes = {"h2": new Element(), "a[href]": new Element()};
    const get = name => section.nodes[`[data-recommendation-${name}]`];
    for (const name of ["description", "grid", "state", "message", "retry", "sign-in", "feedback", "card"]) {
        assert.match(markup, new RegExp(`data-recommendation-${name}(?![-\\w])`));
        section.nodes[`[data-recommendation-${name}]`] = new Element(true);
    }
    section.nodes["[name=csrfmiddlewaretoken]"] = {value: "test-token"};
    get("card").content = {firstElementChild: {cloneNode: makeCard}};
    get("grid").nodes = {};
    const document = {querySelector: selector => selector === "[data-recommendations]" ? section : null};
    const calls = [];
    code.runInNewContext({
        document, URL, URLSearchParams, window: {location: {href: "http://localhost/"}},
        fetch: async (url, options) => {
            calls.push({url, options});
            assert.ok(replies.length, "Unexpected request");
            const reply = replies.shift();
            return typeof reply === "function" ? reply() : reply;
        },
    });
    return {calls, get, document, title: section.nodes.h2};
}

test("shows asynchronous loading and renders personalized data as text", async () => {
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const {get, calls} = setup(pending);
    assert.equal(get("message").textContent, "Loading recommendations…");
    assert.equal(get("grid").attributes["aria-busy"], "true");
    assert.equal(calls[0].url, "/recommendations/data/");
    finish(response({mode: "personalized", items: [localGame({
        title: "<strong>Real title</strong>", genres: ["Adventure"], release_year: 2020,
        cover: "https://example.test/cover.jpg", match_score: 95, reason: "Matches your preferred genres.",
    })]}));
    await nextTick();

    const card = get("grid").children[0];
    assert.equal(card.querySelector("[data-card-link]").textContent, "<strong>Real title</strong>");
    assert.equal(card.querySelector("[data-card-meta]").textContent, "Adventure · 2020");
    assert.equal(card.querySelector("[data-card-match]").textContent, "95% Match");
    assert.equal(card.querySelector("[data-card-reason]").textContent, "Matches your preferred genres.");
    assert.equal(card.querySelector("[data-card-cover]").src, "https://example.test/cover.jpg");
    assert.equal(get("grid").attributes["aria-busy"], "false");
    card.querySelector("[data-card-cover]").fire("error");
    assert.equal(card.querySelector("[data-card-cover]").hidden, true);
    assert.equal(card.querySelector("[data-card-placeholder]").hidden, false);
});

test("labels the general selection and hides absent or unavailable information", async () => {
    const {get, title} = setup(response({mode: "general", items: [
        localGame({cover: "javascript:alert(1)", match_score: 99, reason: "Unexpected reason"}),
    ]}));
    await nextTick();
    const card = get("grid").children[0];
    assert.equal(title.textContent, "Catalog Suggestions");
    assert.equal(card.querySelector("[data-card-meta]").hidden, true);
    assert.equal(card.querySelector("[data-card-match]").hidden, true);
    assert.equal(card.querySelector("[data-card-reason]").hidden, true);
    assert.equal(card.querySelector("[data-card-placeholder]").hidden, false);
    assert.equal(card.querySelector("[data-card-cover]").src, undefined);
});

test("offers retry after a load error and shows empty results after recovery", async () => {
    const {get, calls} = setup(response({}, 503), response({mode: "general", items: []}));
    await nextTick();
    assert.equal(get("retry").hidden, false);
    assert.match(get("message").textContent, /Please try again/);
    await get("retry").fire("click");
    assert.equal(get("retry").hidden, true);
    assert.match(get("message").textContent, /No eligible games/);
    assert.equal(get("grid").hidden, true);
    assert.equal(calls.length, 2);
});

test("shows a sign-in link for an expired session", async () => {
    const {get} = setup(response({}, 401));
    await nextTick();
    assert.equal(get("sign-in").hidden, false);
    assert.equal(get("retry").hidden, true);
});

test("adds an external game once with its token and CSRF and keeps the other cards", async () => {
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const {get, calls} = setup(response({mode: "personalized", items: [
        localGame({title: "External Game", game_url: null, add_url: "/recommendations/igdb/2/add/",
                   add_token: "signed-ticket", match_score: null}),
        localGame(),
    ]}), pending);
    await nextTick();
    const [card, other] = get("grid").children;
    assert.equal(card.querySelector("[data-card-link]").hidden, true);
    assert.equal(card.querySelector("[data-card-match]").hidden, true);
    const button = card.querySelector("[data-card-add]");
    const adding = button.fire("click");
    assert.equal(button.disabled, true);
    await button.fire("click");
    assert.equal(calls.length, 2);
    assert.equal(calls[1].options.method, "POST");
    assert.equal(calls[1].options.headers["X-CSRFToken"], "test-token");
    assert.equal(calls[1].options.body.get("token"), "signed-ticket");
    finish(response({success: true, created: true}));
    await adding;
    assert.deepEqual(get("grid").children, [other]);
    assert.match(get("feedback").textContent, /was added to My Library/);
    assert.equal(calls.length, 2);
});

test("keeps a failed local card retryable and handles an already saved result", async () => {
    const {get, calls} = setup(
        response({mode: "general", items: [localGame()]}),
        response({error: "Please try again later."}, 503),
        response({success: true, created: false}),
    );
    await nextTick();
    const [card] = get("grid").children;
    const button = card.querySelector("[data-card-add]");
    await button.fire("click");
    const error = card.querySelector("[data-card-error]");
    assert.equal(error.textContent, "Please try again later.");
    assert.equal(error.hidden, false);
    assert.equal(button.disabled, false);
    assert.equal(calls[1].url, "/games/1/add-to-library/");
    assert.equal(calls[1].options.body, undefined);
    await button.fire("click");
    assert.equal(get("grid").children.length, 0);
    assert.match(get("feedback").textContent, /already in My Library/);
    assert.match(get("message").textContent, /No more recommendations/);
    assert.equal(calls.length, 3);
});
