
document.addEventListener("DOMContentLoaded", () => {

    const app = document.getElementById("libraryApp");
    if (!app) return;

    const grid = document.getElementById("libraryGrid");
    const scrollSentinel = document.getElementById("libraryScrollSentinel");
    const noResults = document.getElementById("libraryNoResults");
    const loadingState = document.getElementById("libraryLoading");
    const errorState = document.getElementById("libraryError");
    const retry = document.getElementById("libraryRetry");

    const totalCount = document.getElementById("totalGames");
    const playingCount = document.getElementById("playingGames");
    const completedCount = document.getElementById("completedGames");
    const steamCount = document.getElementById("steamGames");
    const playtimeCount = document.getElementById("totalPlaytime");
    const plannedCount = document.getElementById("plannedGames");
    const mostPlayedList = document.getElementById("libraryMostPlayed");
    const mostPlayedEmpty = document.getElementById("libraryMostPlayedEmpty");
    const recentlyAddedList = document.getElementById("libraryRecentlyAdded");
    const recentlyAddedEmpty = document.getElementById("libraryRecentlyAddedEmpty");

    const filterButtons = document.querySelectorAll(".library-filter");
    const searchInput = document.getElementById("librarySearch");
    const genreSelect = document.getElementById("libraryGenre");
    const sortSelect = document.getElementById("librarySort");
    const clearFilters = document.getElementById("libraryClearFilters");
    const resultCount = document.getElementById("libraryResultCount");

    const editModal = document.getElementById("libraryEditModal");
    const editTitle = document.getElementById("libraryEditGameTitle");
    const statusSelect = document.getElementById("libraryStatusSelect");
    const editSave = document.getElementById("libraryEditSave");
    const editCancel = document.getElementById("libraryEditCancel");
    const editError = document.getElementById("libraryEditError");

    const removeModal = document.getElementById("libraryRemoveModal");
    const removeDescription = document.getElementById("libraryRemoveDescription");
    const removeConfirm = document.getElementById("libraryRemoveConfirm");
    const removeCancel = document.getElementById("libraryRemoveCancel");
    const removeError = document.getElementById("libraryRemoveError");

    const csrfToken = document.querySelector(
        "#libraryCsrfForm input[name='csrfmiddlewaretoken']"
    ).value;

    let games = [];
    let matchingGames = [];
    let renderedCount = 0;
    let statusChoices = [];
    let activeFilter = "all";
    let libraryLoaded = false;
    let selectedGame = null;
    let busy = false;

    const batchSize = 24;
    const observer = new IntersectionObserver(entries => {
        if (entries.at(-1)?.isIntersecting) appendGames();
    }, { rootMargin: "0px 0px 200px 0px" });

    const titleOrder = new Intl.Collator("en", { numeric: true, sensitivity: "base" });
    const hoursFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
    const dateFormat = new Intl.DateTimeFormat("en", { dateStyle: "medium" });


    // CREATE HTML ELEMENT
    function element(tag, className, text) {
        const node = document.createElement(tag);

        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;

        return node;
    }


    function updateStats() {
        const steamGames = games.filter(game => game.is_on_steam);
        const knownTimes = steamGames
            .map(game => game.playtime_minutes)
            .filter(Number.isFinite);
        const totalMinutes = knownTimes.reduce((total, minutes) => total + minutes, 0);

        totalCount.textContent = games.length;
        steamCount.textContent = steamGames.length;
        playingCount.textContent = games.filter(game => game.status === "playing").length;
        completedCount.textContent = games.filter(game => game.status === "completed").length;
        plannedCount.textContent = games.filter(game => game.status === "plan_to_play").length;
        playtimeCount.textContent = steamGames.length > 0 && knownTimes.length === 0
            ? "Unavailable"
            : `${hoursFormat.format(totalMinutes / 60)}h`;
    }

    function sortGames(items, sort) {
        if (sort !== "most_played" && sort !== "least_played") {
            const direction = sort === "title_desc" ? -1 : 1;
            return items.sort((first, second) => direction * titleOrder.compare(first.title, second.title));
        }

        const direction = sort === "most_played" ? -1 : 1;
        return items.sort((first, second) => {
            const firstKnown = Number.isFinite(first.playtime_minutes);
            const secondKnown = Number.isFinite(second.playtime_minutes);
            if (firstKnown !== secondKnown) {
                return firstKnown ? -1 : 1;
            }
            if (firstKnown && first.playtime_minutes !== second.playtime_minutes) {
                return direction * (first.playtime_minutes - second.playtime_minutes);
            }
            return titleOrder.compare(first.title, second.title);
        });
    }

    function updateGenreOptions() {
        const selectedGenre = genreSelect.value;
        const genres = new Map();
        for (const game of games) {
            for (const genre of game.genres) {
                const key = genre.toLowerCase();
                if (!genres.has(key)) genres.set(key, genre);
            }
        }

        // Keep the active genre if a library action removed its last game.
        if (selectedGenre && !genres.has(selectedGenre)) {
            genres.set(selectedGenre, genreSelect.selectedOptions[0].textContent);
        }

        const allGenres = element("option", "", "All Genres");
        allGenres.value = "";
        const options = [...genres.values()].sort(titleOrder.compare).map(genre => {
            const option = element("option", "", genre);
            option.value = genre.toLowerCase();
            return option;
        });
        genreSelect.replaceChildren(allGenres, ...options);
        genreSelect.value = selectedGenre;
    }

    function createCover(game, className) {
        const cover = element("span", className);
        const placeholder = element("span", "library-image-placeholder", "G");
        placeholder.setAttribute("aria-hidden", "true");
        cover.appendChild(placeholder);

        if (game.image) {
            const image = document.createElement("img");
            image.alt = "";
            image.loading = "lazy";
            image.decoding = "async";
            image.addEventListener("error", () => image.remove(), { once: true });
            image.src = game.image;
            cover.appendChild(image);
        }

        return cover;
    }

    function renderHighlightList(list, empty, items, description, ranked = false) {
        const entries = items.map((game, index) => {
            const entry = element("li", "library-highlight-item");
            if (ranked) {
                const rank = element("span", "library-highlight-rank", index + 1);
                rank.setAttribute("aria-hidden", "true");
                entry.appendChild(rank);
            }

            const url = game.details_url || game.steam_url;
            const gameInfo = element(url ? "a" : "span", "library-highlight-game");
            if (url) gameInfo.href = url;
            const title = element("span", "library-highlight-name", game.title);
            title.title = game.title;
            gameInfo.append(createCover(game, "library-highlight-cover"), title);
            entry.append(gameInfo, element("span", "library-highlight-meta", description(game)));
            return entry;
        });
        list.replaceChildren(...entries);
        empty.hidden = entries.length > 0;
    }

    function updateHighlights() {
        const played = games.filter(game => game.is_on_steam && game.playtime_minutes > 0);
        const mostPlayed = sortGames(played, "most_played").slice(0, 3);
        renderHighlightList(mostPlayedList, mostPlayedEmpty, mostPlayed, game => game.playtime_display, true);

        const recentlyAdded = games
            .filter(game => game.added_at)
            .sort((first, second) => Date.parse(second.added_at) - Date.parse(first.added_at))
            .slice(0, 3);
        renderHighlightList(
            recentlyAddedList,
            recentlyAddedEmpty,
            recentlyAdded,
            game => dateFormat.format(new Date(game.added_at))
        );
    }


    function createCard(game) {
        const card = element("article", "library-card");
        card.dataset.status = game.status || "not_added";

        const artwork = element("div", "library-card-artwork");
        artwork.appendChild(createCover(game, "library-card-image"));

        const content = element("div", "library-card-info");
        const title = element("h2", "library-card-title", game.title);
        title.title = game.title;
        const genre = element("span", "library-card-genre", game.genre || "Game");
        genre.title = genre.textContent;
        const status = element("span", "library-status", game.status_display || "Not Added");
        status.title = "GameHub Status";

        const playtime = element("div", "library-playtime");
        playtime.append(
            element("span", "", "Steam Playtime"),
            element("strong", "library-playtime-value", game.playtime_display || "Not connected")
        );
        content.append(title, genre, status, playtime);
        card.append(artwork, content);

        const actions = [];
        if (game.is_on_steam && !game.status && game.add_to_library_url) {
            const button = element("button", "", "+ Add to GameHub");
            button.type = "button";
            button.addEventListener("click", () => addSteamGameToLibrary(game, button));
            actions.push(button);
        }
        if (game.details_url) {
            const link = element("a", "", "View Details");
            link.href = game.details_url;
            actions.push(link);
        }
        if (game.edit_url) {
            const button = element("button", "", "Edit Status");
            button.type = "button";
            button.addEventListener("click", () => openEditModal(game));
            actions.push(button);
        }
        if (game.steam_url) {
            const link = element("a", "", "View on Steam");
            link.href = game.steam_url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            actions.push(link);
        }
        if (game.remove_url) {
            const button = element("button", "library-danger-action", "Remove from Library");
            button.type = "button";
            button.addEventListener("click", () => openRemoveModal(game));
            actions.push(button);
        }

        if (actions.length === 0) return card;

        const footer = element("div", "library-card-footer");
        const primaryAction = actions.shift();
        primaryAction.classList.add("library-primary-action");
        footer.appendChild(primaryAction);

        if (actions.length > 0) {
            const menu = element("details", "library-action-menu");
            const trigger = element("summary", "library-menu-trigger", "⋯");
            trigger.setAttribute("aria-label", "More actions for " + game.title);

            const dropdown = element("div", "library-menu-dropdown");
            for (const action of actions) {
                action.classList.add("library-menu-item");
                action.addEventListener("click", () => { menu.open = false; });
                dropdown.appendChild(action);
            }

            menu.addEventListener("keydown", event => {
                if (event.key === "Escape" && menu.open) {
                    menu.open = false;
                    trigger.focus({ preventScroll: true });
                }
            });

            menu.append(trigger, dropdown);
            footer.appendChild(menu);
        }

        card.appendChild(footer);
        return card;
    }

    function observeRemainingGames() {
        observer.disconnect();
        observer.takeRecords();
        if (renderedCount < matchingGames.length) {
            observer.observe(scrollSentinel);
        }
    }

    function appendGames(limit = batchSize) {
        const nextGames = matchingGames.slice(renderedCount, renderedCount + limit);
        grid.append(...nextGames.map(createCard));
        renderedCount += nextGames.length;

        // Reobserve so a sentinel that stays in view can trigger the next batch.
        observeRemainingGames();
    }

    function renderGames(preserveView = false) {
        if (!libraryLoaded) return;

        const limit = preserveView ? Math.max(batchSize, renderedCount) : batchSize;
        const scrollPosition = preserveView
            ? { left: window.scrollX, top: window.scrollY, behavior: "instant" }
            : null;
        const query = searchInput.value.trim().toLowerCase();
        const genre = genreSelect.value;
        matchingGames = games.filter(game => (
            game.title.toLowerCase().includes(query)
            && (activeFilter === "all" || (game.status || "not_added") === activeFilter)
            && (!genre || game.genres.some(name => name.toLowerCase() === genre))
        ));
        sortGames(matchingGames, sortSelect.value);

        const count = matchingGames.length;
        resultCount.textContent = `${count} ${count === 1 ? "game" : "games"} found`;
        clearFilters.disabled = !(searchInput.value || genre || activeFilter !== "all");
        noResults.hidden = games.length === 0 || count > 0;

        renderedCount = 0;
        grid.replaceChildren();
        if (games.length === 0) {

            const empty = element("div", "library-feedback");
            const explore = element("a", "library-button", "Explore Games");
            empty.append(
                element("h2", "", "Your library is empty"),
                element("p", "", "Add games from the catalog to start your collection.")
            );

            explore.href = app.dataset.gamesUrl;
            empty.appendChild(explore);

            grid.appendChild(empty);
        }

        appendGames(limit);
        if (scrollPosition) window.scrollTo(scrollPosition);
    }


    // LOAD DATA FROM DJANGO
    async function loadLibrary(preserveView = false) {
        loadingState.hidden = libraryLoaded;
        errorState.hidden = true;
        grid.setAttribute("aria-busy", "true");

        try {

            const response = await fetch(app.dataset.apiUrl, {
                headers: {
                    "Accept": "application/json"
                }
            });

            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }

            if (!response.ok) {
                throw new Error("Unable to load library");
            }

            const data = await response.json();

            games = data.games.map(game => ({
                ...game,
                genres: (game.genre || "").split(",")
                    .map(genre => genre.trim())
                    .filter(genre => genre && !["unknown", "steam game"].includes(genre.toLowerCase()))
            }));
            statusChoices = data.status_choices;

            updateGenreOptions();
            updateStats();
            updateHighlights();
            libraryLoaded = true;
            renderGames(preserveView);

        } catch (error) {
            libraryLoaded = false;
            matchingGames = [];
            renderedCount = 0;
            observeRemainingGames();
            noResults.hidden = true;
            resultCount.textContent = "";
            grid.replaceChildren();
            errorState.hidden = false;

            console.error(error);

        } finally {
            loadingState.hidden = true;
            grid.setAttribute("aria-busy", "false");
        }
    }


    // POST DATA TO DJANGO
    async function sendPost(url, data = {}) {

        const body = new URLSearchParams(data);

        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "X-CSRFToken": csrfToken,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            },
            body: body.toString()
        });

        if (response.redirected) {
            window.location.assign(response.url);
            throw new Error("Authentication required");
        }

        if (!response.ok) {
            throw new Error("Request failed");
        }

        return response.json();
    }

     // ADD STEAM GAME TO GAMEHUB
  async function addSteamGameToLibrary(game, button) {

    if (busy) return;

    busy = true;
    button.disabled = true;
    button.textContent = "Adding...";

    try {

        const result = await sendPost(
            game.add_to_library_url
        );

        if (!result.success) {
            throw new Error(
                result.error || "Unable to add game."
            );
        }

        // Reload the library with updated backend data
        await loadLibrary(true);

    } catch (error) {

        button.disabled = false;
        button.textContent = "+ Add to GameHub";

        console.error(error);

        alert(
            "Unable to add this game to GameHub. Please try again."
        );

    } finally {

        busy = false;

    }
 }

    // CLOSE MODALS
    function closeModals() {

        if (busy) return;

        editModal.hidden = true;
        removeModal.hidden = true;
        selectedGame = null;
    }


    // OPEN EDIT MODAL
    function openEditModal(game) {
        if (busy) return;

        selectedGame = game;
        editTitle.textContent = game.title;
        editError.hidden = true;

        statusSelect.replaceChildren();

        statusChoices.forEach(choice => {

            const option = document.createElement("option");
            option.value = choice.value;
            option.textContent = choice.label;

            statusSelect.appendChild(option);
        });

        statusSelect.value = game.status;

        editModal.hidden = false;
        statusSelect.focus({ preventScroll: true });
    }


    // SAVE STATUS
    editSave.addEventListener("click", async () => {

        if (!selectedGame || busy) return;

        busy = true;
        editSave.disabled = true;
        editSave.textContent = "Saving...";
        editError.hidden = true;

        try {

            const result = await sendPost(
                selectedGame.edit_url,
                { status: statusSelect.value }
            );

            selectedGame.status = result.status;
            selectedGame.status_display = result.status_display;

            busy = false;
            closeModals();
            updateStats();
            renderGames(true);

        } catch (error) {

            editError.textContent =
                "Unable to save changes. Please try again.";

            editError.hidden = false;
            console.error(error);

        } finally {

            busy = false;
            editSave.disabled = false;
            editSave.textContent = "Save Changes";

        }
    });


    // OPEN REMOVE MODAL
    function openRemoveModal(game) {
        if (busy) return;

        selectedGame = game;
        removeError.hidden = true;

        removeDescription.textContent =
            `Are you sure you want to remove "${game.title}" from your library?`;

        removeModal.hidden = false;
        removeCancel.focus({ preventScroll: true });
    }


    // CONFIRM REMOVE
    removeConfirm.addEventListener("click", async () => {

        if (!selectedGame || busy) return;

        busy = true;
        removeConfirm.disabled = true;
        removeConfirm.textContent = "Removing...";
        removeError.hidden = true;

        try {

            await sendPost(selectedGame.remove_url);
            await loadLibrary(true);

            busy = false;
            closeModals();

        } catch (error) {

            removeError.textContent =
                "Unable to remove this game. Please try again.";

            removeError.hidden = false;
            console.error(error);

        } finally {

            busy = false;
            removeConfirm.disabled = false;
            removeConfirm.textContent = "Remove Game";

        }
    });


    // CANCEL BUTTONS
    editCancel.addEventListener("click", closeModals);
    removeCancel.addEventListener("click", closeModals);


    // CLICK OUTSIDE MODAL
    [editModal, removeModal].forEach(modal => {

        modal.addEventListener("click", event => {
            if (event.target === modal) {
                closeModals();
            }
        });

    });


    // ESCAPE KEY
    document.addEventListener("keydown", event => {

        if (event.key === "Escape") {
            closeModals();
        }

    });


    function updateStatusButtons() {
        filterButtons.forEach(button => {
            const isActive = button.dataset.filter === activeFilter;
            button.classList.toggle("active", isActive);
            button.setAttribute("aria-pressed", String(isActive));
        });
    }

    filterButtons.forEach(button => {
        button.addEventListener("click", () => {
            activeFilter = button.dataset.filter;
            updateStatusButtons();
            renderGames();
        });
    });

    searchInput.addEventListener("input", () => renderGames());
    sortSelect.addEventListener("change", () => renderGames());
    genreSelect.addEventListener("change", () => {
        updateGenreOptions();
        renderGames();
    });

    clearFilters.addEventListener("click", () => {
        searchInput.value = "";
        genreSelect.value = "";
        activeFilter = "all";
        updateGenreOptions();
        updateStatusButtons();
        renderGames();
        searchInput.focus();
    });

    retry.addEventListener("click", () => loadLibrary());

    document.addEventListener("click", event => {
        for (const menu of grid.querySelectorAll(".library-action-menu[open]")) {
            if (!menu.contains(event.target)) menu.open = false;
        }
    });

    return loadLibrary();

});
