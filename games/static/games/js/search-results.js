
document.addEventListener("DOMContentLoaded", function () {

    const app = document.getElementById("search-page-app");

    if (!app) return;

    const searchInput = document.getElementById("search-page-query");
    const genreSelect = document.getElementById("search-page-genre");
    const sortSelect = document.getElementById("search-page-sort");
    const resultsGrid = document.getElementById("search-page-grid");
    const resultCount = document.getElementById("search-page-count");
    const searchForm = document.getElementById("search-page-form");

    const apiUrl = app.dataset.apiUrl;

    const initialParams = new URLSearchParams(window.location.search);
    const initialGenre = initialParams.get("genre") || "";

    sortSelect.value = initialParams.get("sort") || "title";

    if (!sortSelect.value) {
        sortSelect.value = "title";
    }

    let genresLoaded = false;
    let currentController = null;
    let debounceTimer = null;


    // CREATE A GAME CARD
    function createGameCard(game) {

        const card = document.createElement("a");
        card.href = game.url;
        card.className = "search-game-card";

        const genre = document.createElement("span");
        genre.className = "game-genre";
        genre.textContent = game.genre;

        const title = document.createElement("h2");
        title.textContent = game.title;

        const description = document.createElement("p");
        description.textContent =
            `${game.developer} • ${game.release_year}`;

        const viewGame = document.createElement("span");
        viewGame.className = "search-view-game";
        viewGame.textContent = "View Game";

        card.appendChild(genre);
        card.appendChild(title);
        card.appendChild(description);
        card.appendChild(viewGame);

        return card;
    }


    // SHOW A MESSAGE WHEN THERE ARE NO RESULTS
    function showMessage(titleText, messageText) {

        resultsGrid.replaceChildren();

        const container = document.createElement("div");
        container.className = "search-empty";

        const title = document.createElement("h2");
        title.textContent = titleText;

        const message = document.createElement("p");
        message.textContent = messageText;

        container.appendChild(title);
        container.appendChild(message);

        resultsGrid.appendChild(container);
    }


    // FILL GENRE FILTER FROM DATABASE
    function populateGenres(genres) {

        if (genresLoaded) return;

        genres.forEach(function (genre) {

            const option = document.createElement("option");

            option.value = genre;
            option.textContent = genre;

            genreSelect.appendChild(option);

        });

        genreSelect.value = initialGenre;

        genresLoaded = true;
    }


    // LOAD GAMES FROM DJANGO API
    async function loadGames() {

        if (currentController) {
            currentController.abort();
        }

        const controller = new AbortController();
        currentController = controller;

        const query = searchInput.value.trim();
        const genre = genresLoaded
            ? genreSelect.value
            : initialGenre;
        const sort = sortSelect.value;

        const params = new URLSearchParams();

        params.set("format", "json");

        if (query) params.set("q", query);
        if (genre) params.set("genre", genre);

        params.set("sort", sort);

        resultCount.textContent = "Loading games...";
        resultsGrid.setAttribute("aria-busy", "true");

        try {

            const response = await fetch(
                `${apiUrl}?${params.toString()}`,
                {
                    signal: controller.signal,
                    headers: {
                        "Accept": "application/json"
                    }
                }
            );

            if (!response.ok) {
                throw new Error("Failed to load games");
            }

            const data = await response.json();

            if (currentController !== controller) return;

            // Fill genres from real database values
            populateGenres(data.genres);

            // Clear previous results
            resultsGrid.replaceChildren();

            if (data.games.length === 0) {

                showMessage(
                    "No games found",
                    "Try another search or filter."
                );

            } else {

                const fragment = document.createDocumentFragment();

                data.games.forEach(function (game) {
                    fragment.appendChild(createGameCard(game));
                });

                resultsGrid.appendChild(fragment);

            }

            resultCount.textContent =
                `${data.count} game${data.count === 1 ? "" : "s"} found`;

            // Keep filters in the browser URL
            const pageUrl = new URL(window.location.href);

            if (query) {
                pageUrl.searchParams.set("q", query);
            } else {
                pageUrl.searchParams.delete("q");
            }

            if (genre) {
                pageUrl.searchParams.set("genre", genre);
            } else {
                pageUrl.searchParams.delete("genre");
            }

            if (sort !== "title") {
                pageUrl.searchParams.set("sort", sort);
            } else {
                pageUrl.searchParams.delete("sort");
            }

            window.history.replaceState(
                null,
                "",
                pageUrl.pathname + pageUrl.search
            );

        } catch (error) {

            if (error.name === "AbortError") return;

            resultCount.textContent = "Unable to load games";

            showMessage(
                "Something went wrong",
                "Please try again."
            );

            console.error(error);

        } finally {

            if (currentController === controller) {
                resultsGrid.setAttribute("aria-busy", "false");
            }

        }
    }


    // SEARCH WHILE TYPING
    searchInput.addEventListener("input", function () {

        clearTimeout(debounceTimer);

        debounceTimer = setTimeout(function () {
            loadGames();
        }, 300);

    });


    // CHANGE GENRE
    genreSelect.addEventListener("change", loadGames);


    // CHANGE SORTING
    sortSelect.addEventListener("change", loadGames);


    // HANDLE ENTER KEY
    searchForm.addEventListener("submit", function (event) {

        event.preventDefault();

        clearTimeout(debounceTimer);
        loadGames();

    });


    // INITIAL LOAD
    loadGames();

});
