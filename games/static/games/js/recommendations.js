(() => {
    const section = document.querySelector("[data-recommendations]");
    if (!section) return;

    const title = section.querySelector("h2");
    const description = section.querySelector("[data-recommendation-description]");
    const grid = section.querySelector("[data-recommendation-grid]");
    const state = section.querySelector("[data-recommendation-state]");
    const message = section.querySelector("[data-recommendation-message]");
    const retry = section.querySelector("[data-recommendation-retry]");
    const signIn = section.querySelector("[data-recommendation-sign-in]");
    const feedback = section.querySelector("[data-recommendation-feedback]");
    const template = section.querySelector("[data-recommendation-card]");
    const csrfToken = section.querySelector("[name=csrfmiddlewaretoken]").value;
    let loading = false;

    function showState(text, action = "") {
        message.textContent = text;
        state.hidden = !text;
        retry.hidden = action !== "retry";
        signIn.hidden = action !== "sign-in";
    }

    async function addGame(game, card, button) {
        if (button.disabled) return;
        const errorMessage = card.querySelector("[data-card-error]");
        button.disabled = true;
        button.textContent = "Adding…";
        errorMessage.hidden = true;

        try {
            const response = await fetch(game.add_url, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-CSRFToken": csrfToken,
                    "X-Requested-With": "XMLHttpRequest",
                },
                body: game.add_token ? new URLSearchParams({token: game.add_token}) : undefined,
            });
            if (response.status === 401 || response.redirected) {
                showState("Sign in again to add games to your library.", "sign-in");
                return;
            }

            const result = await response.json().catch(() => null);
            if (!response.ok || result?.success !== true) {
                errorMessage.textContent = result?.error || "Unable to add the game. Please try again.";
                errorMessage.hidden = false;
                return;
            }

            const focusWasOnButton = document.activeElement === button;
            card.remove();
            feedback.textContent = result.created ? `${game.title} was added to My Library.`
                : `${game.title} is already in My Library.`;
            feedback.hidden = false;
            if (!grid.children.length) {
                grid.hidden = true;
                showState("No more recommendations to show.");
            }
            if (focusWasOnButton) {
                const nextButton = grid.querySelector("[data-card-add]:enabled");
                (nextButton || section.querySelector('a[href]')).focus({preventScroll: true});
            }
        } catch {
            errorMessage.textContent = "Unable to add the game. Please try again.";
            errorMessage.hidden = false;
        } finally {
            button.disabled = false;
            button.textContent = "Add to Library";
        }
    }

    function gameCard(game, personalized) {
        const card = template.content.firstElementChild.cloneNode(true);
        const link = card.querySelector("[data-card-link]");
        const name = card.querySelector("[data-card-title]");
        if (game.game_url) {
            link.href = game.game_url;
            link.textContent = game.title;
            link.hidden = false;
            name.hidden = true;
        } else {
            name.textContent = game.title;
        }

        const image = card.querySelector("[data-card-cover]");
        const fallback = card.querySelector("[data-card-placeholder]");
        if (game.cover) {
            try {
                const url = new URL(game.cover, window.location.href);
                if (url.protocol === "https:" || url.protocol === "http:") {
                    image.addEventListener("error", () => {
                        image.hidden = true;
                        fallback.hidden = false;
                    }, {once: true});
                    image.hidden = false;
                    fallback.hidden = true;
                    image.src = url.href;
                }
            } catch {
                // Keep the placeholder for an invalid cover URL.
            }
        }

        const meta = card.querySelector("[data-card-meta]");
        const genres = Array.isArray(game.genres) ? game.genres.join(", ") : "";
        const year = Number.isInteger(game.release_year) && game.release_year > 0 ? game.release_year : "";
        meta.textContent = [genres, year].filter(Boolean).join(" · ");
        meta.hidden = !meta.textContent;

        const match = card.querySelector("[data-card-match]");
        if (personalized && Number.isInteger(game.match_score) && game.match_score >= 0 && game.match_score <= 100) {
            match.textContent = `${game.match_score}% Match`;
            match.hidden = false;
        }
        if (personalized && game.reason) {
            const reason = card.querySelector("[data-card-reason]");
            reason.textContent = game.reason;
            reason.hidden = false;
        }

        const button = card.querySelector("[data-card-add]");
        button.addEventListener("click", () => addGame(game, card, button));
        return card;
    }

    async function loadRecommendations() {
        if (loading) return;
        loading = true;
        grid.setAttribute("aria-busy", "true");
        showState("Loading recommendations…");

        try {
            const response = await fetch(section.dataset.url, {
                credentials: "same-origin",
                headers: {"Accept": "application/json"},
            });
            if (response.status === 401 || response.redirected) {
                showState("Sign in to see your recommendations.", "sign-in");
                return;
            }
            if (!response.ok) throw new Error("Unable to load recommendations.");
            const result = await response.json();
            const personalized = result.mode === "personalized";
            if ((!personalized && result.mode !== "general") || !Array.isArray(result.items)) {
                throw new Error("Invalid recommendations response.");
            }

            title.textContent = personalized ? "Recommended Games" : "Catalog Suggestions";
            description.textContent = personalized ? "Based on your gaming activity."
                : "General picks from the GameHub catalog.";
            description.hidden = false;
            grid.replaceChildren(...result.items.map(game => gameCard(game, personalized)));
            grid.hidden = !result.items.length;
            showState(result.items.length ? "" : "No eligible games to show right now.");
        } catch {
            showState("Unable to load games. Please try again.", "retry");
        } finally {
            loading = false;
            grid.setAttribute("aria-busy", "false");
        }
    }

    retry.addEventListener("click", loadRecommendations);
    loadRecommendations();
})();
