const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const { test } = require("node:test");
const { Script } = require("node:vm");

const template = readFileSync(join(__dirname, "../../../templates/games/library.html"), "utf8");
const libraryScript = new Script(
    readFileSync(join(__dirname, "library.js"), "utf8"),
    { filename: "library.js" }
);

class TestElement {
    constructor() {
        this.children = [];
        this.dataset = {};
        this.attributes = {};
        this.listeners = {};
        this.value = "";
        this.hidden = false;
        this.disabled = false;
        const classes = new Set();
        this.classList = {
            add: name => classes.add(name),
            toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name)
        };
    }

    set textContent(text) {
        this.text = String(text);
    }

    get textContent() {
        return this.text || "";
    }

    get childElementCount() {
        return this.children.length;
    }

    get selectedOptions() {
        return this.children.filter(option => option.value === this.value);
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    addEventListener(event, callback) {
        (this.listeners[event] ??= []).push(callback);
    }

    async dispatch(event) {
        if (this.disabled) return;
        for (const callback of this.listeners[event] || []) await callback({ target: this });
    }

    appendChild(child) {
        this.children.push(child);
    }

    append(...children) {
        this.children.push(...children);
    }

    replaceChildren(...children) {
        this.children = children;
    }

    focus() {}
}

function findElement(element, matches) {
    if (matches(element)) return element;
    for (const child of element.children) {
        const match = findElement(child, matches);
        if (match) return match;
    }
}

function game(title, fields = {}) {
    return {
        title,
        genre: "Unknown",
        status: "plan_to_play",
        status_display: "Plan to Play",
        is_on_steam: false,
        playtime_minutes: null,
        added_at: null,
        ...fields,
    };
}

function numberedGames(count, fields) {
    return Array.from({ length: count }, (_, index) => game(`Game ${index + 1}`, fields));
}

async function loadLibrary(games, writeGame, { failInitialLoad = false } = {}) {
    const elements = Object.fromEntries(
        [...template.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, new TestElement()])
    );
    for (const id of ["libraryGenre", "librarySort"]) {
        const select = new RegExp(`<select id="${id}">\\s*<option value="([^"]*)"`);
        elements[id].value = template.match(select)[1];
    }

    const statusButtons = new Map(
        [...template.matchAll(/<button\b[^>]*data-filter="([^"]+)"/g)].map(([, status]) => {
            const button = new TestElement();
            button.dataset.filter = status;
            return [status, button];
        })
    );
    elements.libraryApp.dataset = { apiUrl: "/library/data/", gamesUrl: "/games/" };

    const document = new TestElement();
    document.getElementById = id => elements[id];
    document.querySelector = () => ({ value: "test-csrf" });
    document.querySelectorAll = () => [...statusButtons.values()];
    document.createElement = () => new TestElement();

    let observer;
    class IntersectionObserver {
        constructor(callback) {
            this.callback = callback;
            this.target = null;
            observer = this;
        }

        observe(target) {
            this.target = target;
            this.wasIntersecting = null;
        }

        disconnect() {
            this.target = null;
        }

        takeRecords() {
            return [];
        }

        intersect(isIntersecting = true) {
            if (!this.target || this.wasIntersecting === isIntersecting) return;
            this.wasIntersecting = isIntersecting;
            this.callback([{ target: this.target, isIntersecting }]);
        }
    }

    const restoredScrollPositions = [];
    const window = {
        scrollX: 0,
        scrollY: 0,
        location: { href: "https://example.test/library/" },
        scrollTo({ left, top }) {
            restoredScrollPositions.push(top);
            this.scrollX = left;
            this.scrollY = top;
        }
    };
    const requests = [];
    const statusChoices = [
        { value: "playing", label: "Playing" },
        { value: "completed", label: "Completed" }
    ];
    const fetch = async (url, options) => {
        requests.push([options.method || "GET", url]);
        if (options.method === "POST") {
            assert.equal(options.headers["X-CSRFToken"], "test-csrf");
            return { ok: true, json: async () => writeGame(url, new URLSearchParams(options.body)) };
        }
        assert.equal(url, elements.libraryApp.dataset.apiUrl);
        if (failInitialLoad && requests.length === 1) return { ok: false };
        return { ok: true, json: async () => ({ games, status_choices: statusChoices }) };
    };

    libraryScript.runInNewContext({
        document, window, IntersectionObserver, fetch, URLSearchParams,
        console: {
            error(error) {
                if (!failInitialLoad || requests.length !== 1) throw error;
                assert.equal(error.message, "Unable to load library");
            }
        },
    });
    await document.dispatch("DOMContentLoaded");

    const cardTitles = () => elements.libraryGrid.children
        .filter(card => card.className === "library-card")
        .map(card => findElement(card, node => node.className === "library-card-title").textContent);
    const highlightTitles = id => elements[id].children.map(row =>
        findElement(row, node => node.className === "library-highlight-name").textContent
    );
    const change = (id, value, event = "change") => {
        elements[id].value = value;
        return elements[id].dispatch(event);
    };
    return {
        elements, statusButtons, observer, cardTitles, highlightTitles, change,
        window, restoredScrollPositions, requests,
    };
}

test("combines status, title and every real genre, then clears filters", async () => {
    const { elements, statusButtons, cardTitles, change } = await loadLibrary([
        game("Cosmic Voyage", { genre: "Strategy, RPG", status: null }),
        game("Cosmic Map", { genre: "rpg", status: "playing" }),
        game("Cosmic Drift", { genre: "Racing", status: null }),
        game("Forest RPG", { genre: "RPG", status: null }),
        game("Missing Genre"),
        game("Steam Game", { genre: "Steam Game", status: null }),
    ]);
    assert.deepEqual(
        elements.libraryGenre.children.map(option => option.value), ["", "racing", "rpg", "strategy"]
    );

    await change("librarySearch", "  COSMIC  ", "input");
    await statusButtons.get("not_added").dispatch("click");
    await change("libraryGenre", "rpg");
    assert.deepEqual(cardTitles(), ["Cosmic Voyage"]);
    assert.equal(elements.libraryResultCount.textContent, "1 game found");
    assert.equal(statusButtons.get("not_added").attributes["aria-pressed"], "true");
    assert.equal(elements.totalGames.textContent, "6");
    assert.equal(elements.playingGames.textContent, "1");

    await change("librarySearch", "No Match", "input");
    assert.equal(elements.libraryResultCount.textContent, "0 games found");
    assert.equal(elements.libraryNoResults.hidden, false);

    await elements.libraryClearFilters.dispatch("click");
    assert.equal(elements.librarySearch.value, "");
    assert.equal(elements.libraryGenre.value, "");
    assert.equal(statusButtons.get("all").attributes["aria-pressed"], "true");
    assert.equal(elements.libraryClearFilters.disabled, true);
    assert.equal(elements.libraryNoResults.hidden, true);
    assert.equal(elements.libraryResultCount.textContent, "6 games found");
});

test("sorts by title and playtime with unknown values last in both directions", async () => {
    const { cardTitles, change } = await loadLibrary([
        game("Zulu"),
        game("Alpha", { is_on_steam: true, playtime_minutes: 0 }),
        game("Delta", { is_on_steam: true, playtime_minutes: 120 }),
        game("Beta", { is_on_steam: true, playtime_minutes: 15 }),
        game("Echo", { is_on_steam: true }),
    ]);
    assert.deepEqual(cardTitles(), ["Alpha", "Beta", "Delta", "Echo", "Zulu"]);
    await change("librarySort", "title_desc");
    assert.deepEqual(cardTitles(), ["Zulu", "Echo", "Delta", "Beta", "Alpha"]);
    await change("librarySort", "most_played");
    assert.deepEqual(cardTitles(), ["Delta", "Beta", "Alpha", "Echo", "Zulu"]);
    await change("librarySort", "least_played");
    assert.deepEqual(cardTitles(), ["Alpha", "Beta", "Delta", "Echo", "Zulu"]);
});

test("appends all 109 games in order without replacing cards or requesting more data", async () => {
    const games = numberedGames(109, { image: "/cover.jpg" });
    const { elements, observer, cardTitles, requests, window } = await loadLibrary(games);
    const initialCards = elements.libraryGrid.children.slice();
    assert.equal(initialCards.length, 24);
    assert.equal(observer.target, elements.libraryScrollSentinel);
    observer.intersect(false);
    assert.equal(cardTitles().length, 24);

    for (const count of [48, 72, 96, 109]) {
        observer.intersect();
        assert.deepEqual(cardTitles(), games.slice(0, count).map(game => game.title));
        assert.equal(elements.libraryResultCount.textContent, "109 games found");
    }
    assert.equal(observer.target, null);
    observer.intersect();
    assert.equal(cardTitles().length, 109);
    initialCards.forEach((card, index) => assert.equal(elements.libraryGrid.children[index], card));
    const lastCard = elements.libraryGrid.children.at(-1);
    const image = findElement(lastCard, node => node.src === "/cover.jpg");
    assert.equal(image.loading, "lazy");
    assert.equal(window.location.href, "https://example.test/library/");
    assert.deepEqual(requests, [["GET", "/library/data/"]]);
});

test("restarts at 24 after search, status, genre, sorting and clear changes", async () => {
    const games = numberedGames(60, { genre: "RPG", status: "playing" });
    const { elements, observer, statusButtons, cardTitles, change } = await loadLibrary(games);
    const actions = [
        () => change("librarySearch", "Game", "input"),
        () => statusButtons.get("playing").dispatch("click"),
        () => change("libraryGenre", "rpg"),
        () => change("librarySort", "title_desc"),
        () => elements.libraryClearFilters.dispatch("click"),
    ];
    for (const action of actions) {
        observer.intersect();
        assert.equal(cardTitles().length, 48);
        await action();
        assert.equal(cardTitles().length, 24);
        assert.equal(elements.libraryResultCount.textContent, "60 games found");
    }
    observer.intersect();
    assert.deepEqual(cardTitles(), games.slice().reverse().slice(0, 48).map(game => game.title));
    assert.equal(elements.totalGames.textContent, "60");
});

test("shows highlights from the full library using only known dates and positive Steam time", async () => {
    const games = Array.from({ length: 30 }, (_, index) => game(`Filler ${index + 1}`));
    games.push(
        game("Latest Added", { added_at: "2026-11-24T12:00:00Z", image: "/recent.jpg" }),
        game("Older Added", { added_at: "2026-11-22T12:00:00Z" }),
        game("Z Most Played", {
            is_on_steam: true, playtime_minutes: 180, playtime_display: "3 hours", image: "/played.jpg"
        }),
        game("Z Second Played", {
            is_on_steam: true, playtime_minutes: 120, playtime_display: "2 hours",
            added_at: "2026-11-23T12:00:00Z"
        }),
        game("Z Third Played", { is_on_steam: true, playtime_minutes: 60, playtime_display: "1 hour" }),
        game("Z Fourth Played", { is_on_steam: true, playtime_minutes: 30, playtime_display: "30 min" }),
        game("Zero Playtime", { is_on_steam: true, playtime_minutes: 0 }),
        game("Missing Playtime", { is_on_steam: true, updated_at: "2026-11-25T12:00:00Z" }),
        game("Oldest Added", { added_at: "2026-11-21T12:00:00Z" })
    );
    const { elements, cardTitles, highlightTitles, change } = await loadLibrary(games);
    assert.equal(cardTitles().includes("Z Most Played"), false);
    await change("librarySearch", "Filler", "input");
    assert.deepEqual(
        highlightTitles("libraryMostPlayed"), ["Z Most Played", "Z Second Played", "Z Third Played"]
    );
    assert.deepEqual(
        highlightTitles("libraryRecentlyAdded"), ["Latest Added", "Z Second Played", "Older Added"]
    );
    const ranks = elements.libraryMostPlayed.children.map(row =>
        findElement(row, node => node.className === "library-highlight-rank").textContent
    );
    assert.deepEqual(ranks, ["1", "2", "3"]);
    for (const [list, image] of [[elements.libraryMostPlayed, "/played.jpg"], [elements.libraryRecentlyAdded, "/recent.jpg"]]) {
        assert.equal(findElement(list.children[0], node => node.src === image).loading, "lazy");
    }
    assert.equal(elements.totalPlaytime.textContent, "6.5h");
});

test("opens the themed error state and loads the library on retry", async () => {
    const { elements, cardTitles, requests } = await loadLibrary(
        [game("Retry Game")], undefined, { failInitialLoad: true }
    );
    assert.equal(elements.libraryError.hidden, false);
    assert.equal(elements.libraryLoading.hidden, true);
    assert.deepEqual(cardTitles(), []);

    await elements.libraryRetry.dispatch("click");
    assert.equal(elements.libraryError.hidden, true);
    assert.equal(elements.libraryLoading.hidden, true);
    assert.deepEqual(cardTitles(), ["Retry Game"]);
    assert.deepEqual(requests, [["GET", "/library/data/"], ["GET", "/library/data/"]]);
});

test("shows empty highlights and keeps a known zero distinct from unavailable time", async () => {
    const unknown = game("Missing Playtime", { is_on_steam: true });
    const allUnknown = await loadLibrary([unknown]);
    assert.equal(allUnknown.elements.totalPlaytime.textContent, "Unavailable");
    assert.equal(allUnknown.elements.libraryMostPlayedEmpty.hidden, false);
    assert.equal(allUnknown.elements.libraryRecentlyAddedEmpty.hidden, false);

    const knownZero = game("Never Played", { is_on_steam: true, playtime_minutes: 0 });
    const { elements, highlightTitles } = await loadLibrary([unknown, knownZero]);
    assert.equal(elements.totalPlaytime.textContent, "0h");
    assert.deepEqual(highlightTitles("libraryMostPlayed"), []);
});

test("adding a Steam game keeps filters, sorting and shown count and refreshes highlights", async () => {
    const games = numberedGames(51, {
        genre: "RPG",
        status: null,
        status_display: "Not Added",
        is_on_steam: true
    });
    games[50].add_to_library_url = "/steam/add/";
    const library = await loadLibrary(games, url => {
        assert.equal(url, "/steam/add/");
        games[50] = game("Game 51", {
            genre: "RPG",
            is_on_steam: true,
            details_url: "/games/1/",
            added_at: "2026-11-24T12:00:00Z"
        });
        return { success: true };
    });
    const {
        elements, statusButtons, observer, cardTitles, highlightTitles, change,
        window, restoredScrollPositions, requests,
    } = library;

    await change("librarySearch", "Game", "input");
    await change("libraryGenre", "rpg");
    await change("librarySort", "title_desc");
    await statusButtons.get("not_added").dispatch("click");
    observer.intersect();
    assert.equal(cardTitles().length, 48);
    window.scrollY = 1240;

    const add = findElement(elements.libraryGrid, node => node.textContent === "+ Add to GameHub");
    await add.dispatch("click");
    assert.equal(cardTitles().length, 48);
    assert.equal(cardTitles()[0], "Game 50");
    assert.equal(elements.librarySearch.value, "Game");
    assert.equal(elements.libraryGenre.value, "rpg");
    assert.equal(elements.librarySort.value, "title_desc");
    assert.equal(statusButtons.get("not_added").attributes["aria-pressed"], "true");
    assert.equal(elements.libraryResultCount.textContent, "50 games found");
    assert.equal(elements.totalGames.textContent, "51");
    assert.deepEqual(highlightTitles("libraryRecentlyAdded"), ["Game 51"]);
    assert.deepEqual(restoredScrollPositions, [1240]);
    assert.deepEqual(requests, [
        ["GET", "/library/data/"], ["POST", "/steam/add/"], ["GET", "/library/data/"]
    ]);

    await elements.libraryClearFilters.dispatch("click");
    assert.equal(cardTitles().length, 24);
    const details = findElement(elements.libraryGrid, node => node.textContent === "View Details");
    assert.equal(details.href, "/games/1/");
});

test("removal keeps available cards visible and stops observing when no matches remain to append", async () => {
    const games = numberedGames(48);
    games.push(game("Last Game", {
        remove_url: "/library/remove/",
        added_at: "2026-11-24T12:00:00Z"
    }));
    const library = await loadLibrary(games, url => {
        assert.equal(url, "/library/remove/");
        games.pop();
        return { success: true };
    });
    const { elements, observer, cardTitles, highlightTitles, window, restoredScrollPositions } = library;

    observer.intersect();
    observer.intersect();
    assert.equal(cardTitles().length, 49);
    assert.equal(cardTitles().at(-1), "Last Game");
    assert.deepEqual(highlightTitles("libraryRecentlyAdded"), ["Last Game"]);
    window.scrollY = 2080;
    const remove = findElement(
        elements.libraryGrid, node => node.textContent === "Remove from Library"
    );
    await remove.dispatch("click");
    await elements.libraryRemoveConfirm.dispatch("click");

    assert.deepEqual(cardTitles(), games.map(game => game.title));
    assert.equal(observer.target, null);
    assert.equal(elements.libraryResultCount.textContent, "48 games found");
    assert.equal(elements.libraryRecentlyAddedEmpty.hidden, false);
    assert.equal(elements.libraryRemoveModal.hidden, true);
    assert.deepEqual(restoredScrollPositions, [2080]);
});

test("removing the last game of the active genre keeps that filter until it changes", async () => {
    const games = [
        game("Adventure Game", { genre: "Adventure" }),
        game("RPG Game", { genre: "RPG", remove_url: "/library/remove-rpg/" })
    ];
    const { elements, cardTitles, change, observer } = await loadLibrary(games, url => {
        assert.equal(url, "/library/remove-rpg/");
        games.pop();
        return { success: true };
    });

    await change("libraryGenre", "rpg");
    const remove = findElement(elements.libraryGrid, node => node.textContent === "Remove from Library");
    await remove.dispatch("click");
    await elements.libraryRemoveConfirm.dispatch("click");
    assert.equal(elements.libraryGenre.value, "rpg");
    assert.equal(elements.libraryResultCount.textContent, "0 games found");
    assert.equal(elements.libraryNoResults.hidden, false);
    assert.deepEqual(cardTitles(), []);
    assert.equal(observer.target, null);

    await change("libraryGenre", "adventure");
    assert.deepEqual(elements.libraryGenre.children.map(option => option.value), ["", "adventure"]);
    assert.deepEqual(cardTitles(), ["Adventure Game"]);
});

test("editing status preserves the filters, shown count and scroll without refetching", async () => {
    const games = numberedGames(51, { status: "playing", status_display: "Playing", genre: "RPG" });
    games[50].edit_url = "/library/edit/";
    const library = await loadLibrary(games, (url, fields) => {
        assert.equal(url, "/library/edit/");
        assert.equal(fields.get("status"), "completed");
        return { success: true, status: "completed", status_display: "Completed" };
    });
    const {
        elements, statusButtons, observer, cardTitles, change, window, restoredScrollPositions, requests,
    } = library;

    await change("libraryGenre", "rpg");
    await change("librarySort", "title_desc");
    await statusButtons.get("playing").dispatch("click");
    observer.intersect();
    assert.equal(cardTitles().length, 48);
    window.scrollY = 1240;
    const edit = findElement(elements.libraryGrid, node => node.textContent === "Edit Status");
    await edit.dispatch("click");
    elements.libraryStatusSelect.value = "completed";
    await elements.libraryEditSave.dispatch("click");

    assert.equal(elements.libraryGenre.value, "rpg");
    assert.equal(elements.librarySort.value, "title_desc");
    assert.equal(statusButtons.get("playing").attributes["aria-pressed"], "true");
    assert.equal(elements.libraryResultCount.textContent, "50 games found");
    assert.equal(cardTitles().length, 48);
    assert.equal(cardTitles()[0], "Game 50");
    assert.equal(elements.playingGames.textContent, "50");
    assert.equal(elements.completedGames.textContent, "1");
    assert.equal(elements.libraryEditModal.hidden, true);
    assert.deepEqual(restoredScrollPositions, [1240]);
    assert.deepEqual(requests, [["GET", "/library/data/"], ["POST", "/library/edit/"]]);
});
