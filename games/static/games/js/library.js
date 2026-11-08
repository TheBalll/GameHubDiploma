
document.addEventListener("DOMContentLoaded", () => {

    const app = document.getElementById("libraryApp");
    if (!app) return;

    const grid = document.getElementById("libraryGrid");
    const noResults = document.getElementById("libraryNoResults");

    const totalCount = document.getElementById("totalGames");
    const playingCount = document.getElementById("playingGames");
    const completedCount = document.getElementById("completedGames");

    const filterButtons = document.querySelectorAll(".library-filter");

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
    let statusChoices = [];
    let activeFilter = "all";
    let selectedGame = null;
    let busy = false;


    // CREATE HTML ELEMENT
    function element(tag, className, text) {
        const node = document.createElement(tag);

        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;

        return node;
    }


    // UPDATE STATISTICS
    function updateStats() {
        totalCount.textContent = games.length;

        playingCount.textContent = games.filter(
            game => game.status === "playing"
        ).length;

        completedCount.textContent = games.filter(
            game => game.status === "completed"
        ).length;
    }


    // CREATE GAME CARD
    function createCard(game) {

        const card = element("div", "library-card");
        card.dataset.status = game.status;

        const content = element("div", "library-card-content");

        const genre = element("span", "game-genre", game.genre);
        const title = element("h2", "", game.title);
        const status = element(
            "span",
            "library-status",
            game.status_display
        );

        const actions = element("div", "library-actions");

        // EDIT BUTTON
        const editButton = element("button", "edit-library-btn", "Edit");
        editButton.type = "button";
        editButton.addEventListener("click", () => openEditModal(game));

        // VIEW DETAILS
        const detailsLink = element("a", "details-btn", "View Details");
        detailsLink.href = game.details_url;

        // REMOVE BUTTON
        const removeButton = element(
            "button",
            "remove-library-btn",
            "Remove"
        );

        removeButton.type = "button";
        removeButton.addEventListener(
            "click",
            () => openRemoveModal(game)
        );

        actions.append(editButton, detailsLink, removeButton);
        content.append(genre, title, status, actions);
        card.appendChild(content);

        return card;
    }


    // RENDER ALL GAMES
    function renderGames() {

        grid.replaceChildren();
        updateStats();

        if (games.length === 0) {

            const empty = element("div", "library-empty");

            empty.appendChild(
                element("h2", "", "Your library is empty")
            );

            empty.appendChild(
                element(
                    "p",
                    "",
                    "Start exploring games and add your favorites to your collection."
                )
            );

            const explore = element(
                "a",
                "library-explore-btn",
                "Explore Games"
            );

            explore.href = app.dataset.gamesUrl;
            empty.appendChild(explore);

            grid.appendChild(empty);
            noResults.hidden = true;
            return;
        }

        const visibleGames = games.filter(game => {
            return activeFilter === "all" ||
                   game.status === activeFilter;
        });

        const fragment = document.createDocumentFragment();

        visibleGames.forEach(game => {
            fragment.appendChild(createCard(game));
        });

        grid.appendChild(fragment);

        noResults.hidden = visibleGames.length > 0;
    }


    // LOAD DATA FROM DJANGO
    async function loadLibrary() {

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

            games = data.games;
            statusChoices = data.status_choices;

            renderGames();

        } catch (error) {

            grid.replaceChildren(
                element(
                    "p",
                    "library-error",
                    "Unable to load your library. Please refresh the page."
                )
            );

            console.error(error);

        } finally {
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


    // CLOSE MODALS
    function closeModals() {

        if (busy) return;

        editModal.hidden = true;
        removeModal.hidden = true;
        selectedGame = null;
    }


    // OPEN EDIT MODAL
    function openEditModal(game) {

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
        statusSelect.focus();
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
            renderGames();

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

        selectedGame = game;
        removeError.hidden = true;

        removeDescription.textContent =
            `Are you sure you want to remove "${game.title}" from your library?`;

        removeModal.hidden = false;
        removeCancel.focus();
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

            games = games.filter(
                game => game.id !== selectedGame.id
            );

            busy = false;
            closeModals();
            renderGames();

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


    // FILTER BUTTONS
    filterButtons.forEach(button => {

        button.addEventListener("click", () => {

            activeFilter = button.dataset.filter;

            filterButtons.forEach(btn => {
                btn.classList.toggle(
                    "active",
                    btn === button
                );
            });

            renderGames();

        });

    });


    // INITIAL LOAD
    loadLibrary();

});
