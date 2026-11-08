
document.addEventListener("DOMContentLoaded", function () {

    const form = document.getElementById("editProfileForm");
    const saveButton = document.getElementById("saveProfileButton");
    const formMessage = document.getElementById("profileFormMessage");

    if (!form || !saveButton || !formMessage) return;

    // AVATAR ELEMENTS
    const avatarInput = document.getElementById("id_avatar");
    const avatarClear = document.getElementById("id_avatar-clear");
    const avatarImage = document.getElementById("avatarPreviewImage");
    const avatarLetter = document.getElementById("avatarPreviewLetter");
    const avatarFileName = document.getElementById("avatarFileName");
    const avatarStatus = document.getElementById("avatarUploadStatus");

    const originalAvatar = avatarImage?.getAttribute("src") || "";
    let previewUrl = null;

    function releasePreview() {
        if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
            previewUrl = null;
        }
    }

    function showOriginalAvatar() {
        releasePreview();

        if (!avatarImage || !avatarLetter) return;

        if (originalAvatar) {
            avatarImage.src = originalAvatar;
            avatarImage.hidden = false;
            avatarLetter.hidden = true;
        } else {
            avatarImage.removeAttribute("src");
            avatarImage.hidden = true;
            avatarLetter.hidden = false;
        }
    }

    // PREVIEW SELECTED AVATAR
    if (avatarInput) {
        avatarInput.addEventListener("change", function () {
            const file = this.files[0];

            if (!file) return;

            if (avatarClear) {
                avatarClear.checked = false;
            }

            releasePreview();
            previewUrl = URL.createObjectURL(file);

            if (avatarImage && avatarLetter) {
                avatarImage.src = previewUrl;
                avatarImage.hidden = false;
                avatarLetter.hidden = true;
            }

            if (avatarFileName) {
                avatarFileName.textContent = file.name;
            }

            if (avatarStatus) {
                avatarStatus.textContent =
                    "New picture selected. Save to apply.";
            }
        });
    }

    // REMOVE AVATAR
    if (avatarClear) {
        avatarClear.addEventListener("change", function () {
            if (this.checked) {
                if (avatarInput) {
                    avatarInput.value = "";
                }

                releasePreview();

                if (avatarImage && avatarLetter) {
                    avatarImage.hidden = true;
                    avatarLetter.hidden = false;
                }

                if (avatarFileName) {
                    avatarFileName.textContent = "JPG, PNG or WebP";
                }

                if (avatarStatus) {
                    avatarStatus.textContent =
                        "Picture will be removed when you save.";
                }
            } else {
                showOriginalAvatar();

                if (avatarStatus) {
                    avatarStatus.textContent = "";
                }
            }
        });
    }

    // MESSAGE HELPER
    function showMessage(message, isSuccess = false) {
        formMessage.textContent = message;
        formMessage.hidden = false;
        formMessage.classList.toggle(
            "success",
            isSuccess
        );
    }

    // CLEAR PREVIOUS ERRORS
    function clearErrors() {
        formMessage.textContent = "";
        formMessage.hidden = true;
        formMessage.classList.remove("success");

        form.querySelectorAll(".profile-js-error").forEach(
            element => element.remove()
        );
    }

    // DISPLAY DJANGO FIELD ERRORS
    function showFieldErrors(errors) {
        Object.entries(errors || {}).forEach(([field, messages]) => {
            const message = messages
                .map(item => item.message)
                .join(" ");

            const input = Array.from(form.elements).find(
                element => element.name === field
            );

            if (!input) {
                showMessage(message);
                return;
            }

            const container = input.closest(".edit-profile-field");

            if (!container) {
                showMessage(message);
                return;
            }

            const errorElement = document.createElement("p");
            errorElement.className = "profile-js-error";
            errorElement.textContent = message;
            errorElement.setAttribute("role", "alert");

            container.appendChild(errorElement);
        });
    }

    // SAVE PROFILE USING AJAX
    form.addEventListener("submit", async function (event) {
        event.preventDefault();

        if (saveButton.disabled) return;

        clearErrors();

        saveButton.disabled = true;
        saveButton.textContent = "Saving...";

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

            const result = await response.json();

            if (!response.ok || !result.success) {
                if (result.errors) {
                    showFieldErrors(result.errors);
                }

                showMessage(
                    result.message || "Unable to update profile."
                );
                return;
            }

            showMessage(
                result.message || "Profile updated successfully!",
                true
            );

            releasePreview();

            window.location.assign(result.redirect_url);

        } catch (error) {
            console.error(error);
            showMessage(
                "Something went wrong. Please try again."
            );
        } finally {
            saveButton.disabled = false;
            saveButton.textContent = "Save Changes";
        }
    });

    window.addEventListener("pagehide", releasePreview);

});
