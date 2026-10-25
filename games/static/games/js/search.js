const searchInput = document.getElementById('game-search');
const gameCards = document.querySelectorAll('.game-card');

searchInput.addEventListener('input', function () {
    const searchText = searchInput.value.toLowerCase();

    gameCards.forEach(function (card) {
        const gameTitle = card.dataset.title;

        if (gameTitle.includes(searchText)) {
            card.style.display = '';
        } else {
            card.style.display = 'none';
        }
    });
});