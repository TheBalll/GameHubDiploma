
document.addEventListener("DOMContentLoaded", function () {

    const form = document.getElementById("addToLibraryForm");
    const button = document.getElementById("addToLibraryButton");
    const message = document.getElementById("addToLibraryMessage");

    if (!form || !button || !message) {
        return;
    }

    form.addEventListener("submit", async function (event) {

        event.preventDefault();

        if (button.disabled) {
            return;
        }

        button.disabled = true;
        button.textContent = "Adding...";
        message.hidden = true;

        try {

            const response = await fetch(form.action, {
                method: "POST",
                credentials: "same-origin",
                body: new FormData(form),
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json"
                }
            });

            if (response.redirected) {
                window.location.assign(response.url);
                return;
            }

            if (!response.ok) {
                throw new Error("Failed to add game.");
            }

            const result = await response.json();

            if (result.success) {

                button.textContent = "Added to Library";
                button.classList.add("added");

                message.textContent = result.message;
                message.hidden = false;

            } else {
                throw new Error("Unexpected server response.");
            }

        } catch (error) {

            console.error(error);

            button.disabled = false;
            button.textContent = "Add to Library";

            message.textContent =
                "Unable to add game. Please try again.";

            message.hidden = false;

        }

    });

});
