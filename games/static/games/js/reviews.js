
document.addEventListener("DOMContentLoaded", function () {

    // ELEMENTS
    const addForm = document.getElementById("addReviewForm");
    const addButton = document.getElementById("submitReviewButton");
    const addError = document.getElementById("reviewFormError");

    const reviewsSection = document.getElementById("reviews");

    const editModal = document.getElementById("editReviewModal");
    const editForm = document.getElementById("editReviewForm");
    const editRating = document.getElementById("editReviewRating");
    const editComment = document.getElementById("editReviewComment");
    const editError = document.getElementById("editReviewError");
    const editSave = document.getElementById("saveEditReview");
    const editCancel = document.getElementById("cancelEditReview");

    let editingReviewId = null;
    let editingReviewButton = null;
    let editBusy = false;


    // HELPERS
    function createElement(tag, className = "", text = undefined) {
        const element = document.createElement(tag);

        if (className) {
            element.className = className;
        }

        if (text !== undefined) {
            element.textContent = text;
        }

        return element;
    }


    function showError(element, message) {
        if (!element) return;

        element.textContent = message;
        element.hidden = false;
    }


    function hideError(element) {
        if (!element) return;

        element.textContent = "";
        element.hidden = true;
    }


    function getValidationMessage(errors) {
        if (!errors) {
            return "Please check your information.";
        }

        const messages = Object.values(errors).flatMap(
            fieldErrors => fieldErrors.map(error => error.message)
        );

        return messages.join(" ") || "Please check your information.";
    }


    // SEND DATA TO DJANGO
    async function submitForm(url, formData) {

        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            body: formData,
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json"
            }
        });

        if (response.redirected) {
            window.location.assign(response.url);
            throw new Error("Authentication required.");
        }

        const contentType = response.headers.get("content-type") || "";

        if (!contentType.includes("application/json")) {
            throw new Error("Unexpected server response.");
        }

        const data = await response.json();

        if (!response.ok) {

            if (data.errors) {
                throw new Error(getValidationMessage(data.errors));
            }

            throw new Error(data.message || "Request failed.");
        }

        if (!data.success) {
            throw new Error(data.message || "Request failed.");
        }

        return data;
    }


    // CREATE REVIEW CARD
    function createReviewCard(review) {

        const card = createElement("div", "review-card");
        card.id = `review-${review.id}`;

        // HEADER
        const header = createElement("div", "review-header");

        // PROFILE LINK
        const profileLink = createElement(
            "a",
            "review-author-link"
        );

        profileLink.href = review.profile_url;
        profileLink.setAttribute(
            "aria-label",
            `View profile of ${review.author}`
        );

        // AVATAR
        const avatar = createElement(
            "span",
            "review-author-avatar"
        );

        if (review.avatar_url) {

            const image = document.createElement("img");
            image.src = review.avatar_url;
            image.alt = "";

            avatar.appendChild(image);

        } else {

            avatar.textContent = review.author
                ? review.author.charAt(0).toUpperCase()
                : "?";
        }

        // USERNAME
        const author = createElement(
            "strong",
            "",
            review.author
        );

        profileLink.append(avatar, author);

        // RATING
        const rating = createElement(
            "span",
            "review-rating",
            `${review.rating}/10`
        );

        header.append(profileLink, rating);

        // COMMENT
        const comment = createElement(
            "p",
            "review-comment",
            review.comment
        );

        // DATE
        const date = createElement(
            "small",
            "",
            review.date
        );

        // ACTIONS
        const actions = createElement(
            "div",
            "review-actions"
        );

        // EDIT BUTTON
        const editLink = createElement(
            "a",
            "edit-review-btn",
            "Edit"
        );

        editLink.href = review.edit_url;

        editLink.dataset.reviewId = review.id;
        editLink.dataset.reviewRating = review.rating;
        editLink.dataset.reviewComment = review.comment;

        // DELETE FORM
        const deleteForm = document.createElement("form");

        deleteForm.method = "post";
        deleteForm.action = review.delete_url;

        const csrfInput = addForm.querySelector(
            "input[name='csrfmiddlewaretoken']"
        ).cloneNode(true);

        deleteForm.appendChild(csrfInput);

        // DELETE BUTTON
        const deleteButton = createElement(
            "button",
            "delete-review-btn",
            "Delete"
        );

        deleteButton.type = "button";

        deleteButton.addEventListener("click", function () {
            window.openDeleteModal(this);
        });

        deleteForm.appendChild(deleteButton);

        actions.append(editLink, deleteForm);

        card.append(header, comment, date, actions);

        return card;
    }


    // ADD REVIEW WITHOUT PAGE RELOAD
    if (addForm && addButton && reviewsSection) {

        addForm.addEventListener("submit", async function (event) {

            event.preventDefault();

            if (addButton.disabled) return;

            addButton.disabled = true;
            addButton.textContent = "Submitting...";

            hideError(addError);

            try {

                const result = await submitForm(
                    addForm.action,
                    new FormData(addForm)
                );

                // REMOVE EMPTY MESSAGE
                const noReviews = reviewsSection.querySelector(
                    ".no-reviews"
                );

                if (noReviews) {
                    noReviews.remove();
                }

                // CREATE NEW REVIEW
                const card = createReviewCard(result.review);

                // ADD AT THE TOP
                const heading = reviewsSection.querySelector("h2");

                heading.insertAdjacentElement("afterend", card);

                // CLEAR FORM
                addForm.reset();

                // SCROLL TO NEW REVIEW
                card.scrollIntoView({
                    behavior: "smooth",
                    block: "center"
                });

            } catch (error) {

                console.error(error);
                showError(addError, error.message);

            } finally {

                addButton.disabled = false;
                addButton.textContent = "Submit Review";

            }

        });

    }


    // OPEN EDIT REVIEW MODAL
    document.addEventListener("click", function (event) {

        const button = event.target.closest(".edit-review-btn");

        if (!button || !editModal || !editForm) {
            return;
        }

        event.preventDefault();

        editingReviewId = button.dataset.reviewId;
        editingReviewButton = button;

        editForm.action = button.href;

        editRating.value = button.dataset.reviewRating;
        editComment.value = button.dataset.reviewComment;

        hideError(editError);

        editModal.hidden = false;
        editRating.focus();

    });


    // CLOSE EDIT REVIEW MODAL
    function closeEditModal() {

        if (editBusy || !editModal) {
            return;
        }

        editModal.hidden = true;

        editingReviewId = null;
        editingReviewButton = null;

    }


    if (editCancel) {
        editCancel.addEventListener("click", closeEditModal);
    }


    if (editModal) {

        editModal.addEventListener("click", function (event) {

            if (event.target === editModal) {
                closeEditModal();
            }

        });

    }


    document.addEventListener("keydown", function (event) {

        if (
            event.key === "Escape" &&
            editModal &&
            !editModal.hidden
        ) {
            closeEditModal();
        }

    });


    // SAVE EDITED REVIEW
    if (editForm && editSave) {

        editForm.addEventListener("submit", async function (event) {

            event.preventDefault();

            if (!editingReviewId || editBusy) {
                return;
            }

            editBusy = true;

            editSave.disabled = true;
            editSave.textContent = "Saving...";

            hideError(editError);

            try {

                const result = await submitForm(
                    editForm.action,
                    new FormData(editForm)
                );

                const card = document.getElementById(
                    `review-${editingReviewId}`
                );

                if (card) {

                    // UPDATE RATING
                    const rating = card.querySelector(
                        ".review-rating"
                    );

                    if (rating) {
                        rating.textContent = `${result.review.rating}/10`;
                    }

                    // UPDATE COMMENT
                    const comment = card.querySelector(
                        ".review-comment"
                    );

                    if (comment) {
                        comment.textContent = result.review.comment;
                    }

                }

                // UPDATE DATA FOR NEXT EDIT
                if (editingReviewButton) {

                    editingReviewButton.dataset.reviewRating =
                        result.review.rating;

                    editingReviewButton.dataset.reviewComment =
                        result.review.comment;

                }

                editBusy = false;
                closeEditModal();

            } catch (error) {

                console.error(error);
                showError(editError, error.message);

            } finally {

                editBusy = false;

                editSave.disabled = false;
                editSave.textContent = "Save Changes";

            }

        });

    }

});
