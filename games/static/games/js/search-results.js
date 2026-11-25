
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
    const loginUrl = app.dataset.loginUrl;
    const canImport = app.dataset.canImport === "true";

    if (
        !searchInput ||
        !genreSelect ||
        !sortSelect ||
        !resultsGrid ||
        !resultCount ||
        !searchForm ||
        !apiUrl
    ) {
        console.error("GameHub search elements are missing.");
        return;
    }

    // INITIAL FILTER VALUES
    const initialParams = new URLSearchParams(
        window.location.search
    );

    const initialGenre = initialParams.get("genre") || "";

    sortSelect.value = initialParams.get("sort") || "title";

    if (!sortSelect.value) {
        sortSelect.value = "title";
    }

    // SEARCH STATE
    let genresLoaded = false;
    let currentController = null;
    let debounceTimer = null;

    // PAGINATION STATE
    let currentPage = 0;
    let hasNextPage = false;
    let isLoading = false;
    let searchVersion = 0;

    // INFINITE SCROLL TRIGGER
    const loadMoreTrigger = document.createElement("div");

    loadMoreTrigger.id = "search-load-more";
    loadMoreTrigger.style.minHeight = "40px";
    loadMoreTrigger.style.textAlign = "center";
    loadMoreTrigger.style.padding = "20px";
    loadMoreTrigger.setAttribute("aria-live", "polite");

    resultsGrid.insertAdjacentElement(
        "afterend",
        loadMoreTrigger
    );

    // RETRY BUTTON
    const retryButton = document.createElement("button");

    retryButton.type = "button";
    retryButton.className = "search-view-game";
    retryButton.textContent = "Try Again";
    retryButton.style.display = "none";

    loadMoreTrigger.appendChild(retryButton);

    function setLoadingMessage(message) {
        loadMoreTrigger.replaceChildren();

        if (message) {
            loadMoreTrigger.textContent = message;
        }
    }

    function showRetryButton() {
        loadMoreTrigger.replaceChildren();

        const message = document.createElement("p");
        message.textContent = "Could not load more games.";

        retryButton.style.display = "inline-block";

        loadMoreTrigger.appendChild(message);
        loadMoreTrigger.appendChild(retryButton);
    }

    // CREATE A GAME CARD
    function createGameCard(game) {
        const isExternal =
            game.source === "igdb" ||
            game.source === "steam";

        const card = document.createElement(
            isExternal ? "div" : "a"
        );

        card.className = "search-game-card";

        if (!isExternal) {
            card.href = game.url;
        }

        // GAME COVER
        const imageContainer = document.createElement("div");
        imageContainer.className = "search-game-image";

        if (game.image) {
            const image = document.createElement("img");

            image.src = game.image;
            image.alt = game.title;
            image.loading = "lazy";

            imageContainer.appendChild(image);
        } else {
            imageContainer.textContent = "No image available";
        }

        // GAME INFORMATION
        const content = document.createElement("div");
        content.className = "search-game-content";

        const genre = document.createElement("span");
        genre.className = "game-genre";
        genre.textContent = game.genre || "Unknown";

        const title = document.createElement("h2");
        title.textContent = game.title;

        const description = document.createElement("p");
        description.textContent =
            `${game.developer || "Unknown"} • ${game.release_year || "N/A"}`;

        content.appendChild(genre);
        content.appendChild(title);
        content.appendChild(description);

        // VIEW GAME
        if (isExternal && !canImport) {
            const link = document.createElement("a");
            link.className = "search-view-game";
            link.href = game.store_url || loginUrl;
            link.textContent = game.store_url ? "View Game" : "Sign In";
            content.appendChild(link);
        } else if (isExternal) {
            const button = document.createElement("button");

            button.type = "button";
            button.className = "search-view-game";
            button.textContent = "View Game";

            button.addEventListener("click", async function () {
                button.disabled = true;
                button.textContent = "Opening...";

                try {
                    const csrfToken = document.querySelector(
                        "#game-import-csrf-form input[name='csrfmiddlewaretoken']"
                    )?.value;

                    if (!csrfToken) {
                        throw new Error("CSRF token not found");
                    }

                    const importUrl = game.source === "steam"
                        ? `/games/import-steam/${game.id}/`
                        : `/games/import-igdb/${game.id}/`;

                    const response = await fetch(importUrl, {
                        method: "POST",
                        credentials: "same-origin",
                        headers: {
                            "X-CSRFToken": csrfToken,
                            "Accept": "application/json"
                        }
                    });

                    if (response.status === 401) {
                        window.location.href = loginUrl;
                        return;
                    }

                    if (!response.ok) {
                        throw new Error(
                            `Could not open game (${response.status})`
                        );
                    }

                    const data = await response.json();

                    if (!data.success || !data.url) {
                        throw new Error("Invalid response");
                    }

                    window.location.href = data.url;

                } catch (error) {
                    console.error(error);

                    button.disabled = false;
                    button.textContent = "Try Again";
                }
            });

            content.appendChild(button);

        } else {
            const viewGame = document.createElement("span");

            viewGame.className = "search-view-game";
            viewGame.textContent = "View Game";

            content.appendChild(viewGame);
        }

        card.appendChild(imageContainer);
        card.appendChild(content);

        return card;
    }

    // SHOW EMPTY OR ERROR MESSAGE
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

    // FILL GENRE FILTER FROM SQLITE
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

    // UPDATE BROWSER URL
    function updateBrowserUrl(query, genre, sort) {
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

        pageUrl.searchParams.delete("page");

        window.history.replaceState(
            null,
            "",
            pageUrl.pathname + pageUrl.search
        );
    }

    // LOAD GAMES WITH PAGINATION
    async function loadGames(loadMore = false) {
        if (loadMore) {
            if (isLoading || !hasNextPage) {
                return;
            }
        } else {
            searchVersion++;

            if (currentController) {
                currentController.abort();
            }

            currentPage = 0;
            hasNextPage = false;

            resultsGrid.replaceChildren();
            setLoadingMessage("");
        }

        const version = searchVersion;
        const controller = new AbortController();

        currentController = controller;
        isLoading = true;

        const nextPage = loadMore
            ? currentPage + 1
            : 1;

        const query = searchInput.value.trim();

        const genre = genresLoaded
            ? genreSelect.value
            : initialGenre;

        const sort = sortSelect.value;

        const params = new URLSearchParams();

        params.set("format", "json");
        params.set("page", String(nextPage));

        if (query) {
            params.set("q", query);
        }

        if (genre) {
            params.set("genre", genre);
        }

        params.set("sort", sort);

        if (!loadMore) {
            resultCount.textContent = "Loading games...";
        }

        setLoadingMessage("Loading games...");
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
                throw new Error(
                    `Failed to load games (${response.status})`
                );
            }

            const data = await response.json();

            // IGNORE OLD SEARCH RESPONSES
            if (version !== searchVersion) {
                return;
            }

            populateGenres(data.genres);

            if (!loadMore) {
                resultsGrid.replaceChildren();
            }

            if (data.games.length === 0 && !loadMore) {
                showMessage(
                    "No games found",
                    "Try another search or filter."
                );
            } else {
                const fragment = document.createDocumentFragment();

                data.games.forEach(function (game) {
                    fragment.appendChild(
                        createGameCard(game)
                    );
                });

                resultsGrid.appendChild(fragment);
            }

            currentPage = data.page;
            hasNextPage = data.has_next;

            resultCount.textContent =
                `${data.count} game${data.count === 1 ? "" : "s"} found`;

            setLoadingMessage("");

            if (!loadMore) {
                updateBrowserUrl(query, genre, sort);
            }

        } catch (error) {
            if (error.name === "AbortError") {
                return;
            }

            if (version !== searchVersion) {
                return;
            }

            console.error(error);

            if (!loadMore) {
                resultCount.textContent = "Unable to load games";

                showMessage(
                    "Something went wrong",
                    "Please try again."
                );

                setLoadingMessage("");
            } else {
                showRetryButton();
            }

        } finally {
            // ONLY THE CURRENT REQUEST MAY RESET LOADING
            if (
                version === searchVersion &&
                currentController === controller
            ) {
                isLoading = false;

                resultsGrid.setAttribute(
                    "aria-busy",
                    "false"
                );
            }
        }
    }

    // RETRY FAILED PAGE
    retryButton.addEventListener("click", function () {
        if (!isLoading && hasNextPage) {
            loadGames(true);
        }
    });

    // AUTOMATIC INFINITE SCROLL
    const observer = new IntersectionObserver(
        function (entries) {
            if (!entries[0].isIntersecting) {
                return;
            }

            // Do not automatically retry failed requests.
            if (retryButton.style.display === "inline-block") {
                return;
            }

            loadGames(true);
        },
        {
            rootMargin: "300px 0px",
            threshold: 0
        }
    );

    observer.observe(loadMoreTrigger);

    // SEARCH WHILE TYPING
    searchInput.addEventListener("input", function () {
        clearTimeout(debounceTimer);

        debounceTimer = setTimeout(function () {
            loadGames();
        }, 300);
    });

    // CHANGE GENRE
    genreSelect.addEventListener("change", function () {
        clearTimeout(debounceTimer);
        loadGames();
    });

    // CHANGE SORTING
    sortSelect.addEventListener("change", function () {
        clearTimeout(debounceTimer);
        loadGames();
    });

    // HANDLE ENTER KEY
    searchForm.addEventListener("submit", function (event) {
        event.preventDefault();

        clearTimeout(debounceTimer);
        loadGames();
    });

    // INITIAL LOAD
    loadGames();
});
