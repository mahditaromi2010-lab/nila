document.addEventListener("click", function (e) {
  var toggle = e.target.closest("[data-toggle]");
  if (toggle) {
    var id = toggle.getAttribute("data-toggle");
    var el = document.getElementById(id);
    if (el) {
      el.classList.toggle("open");
      var expanded = el.classList.contains("open");
      toggle.setAttribute("aria-expanded", expanded);
    }
  }
  var dismiss = e.target.closest("[data-dismiss]");
  if (dismiss) {
    var flash = dismiss.closest(".flash");
    if (flash) flash.remove();
  }
});

document.querySelectorAll(".flash").forEach(function (el) {
  setTimeout(function () { el.style.transition = "opacity .4s"; el.style.opacity = "0"; setTimeout(function(){ el.remove(); }, 400); }, 6000);
});

// method card selection on checkout
document.querySelectorAll(".method-card").forEach(function (card) {
  card.addEventListener("click", function () {
    document.querySelectorAll(".method-card").forEach(function (c) { c.classList.remove("selected"); });
    card.classList.add("selected");
    var input = card.querySelector("input[type=radio]");
    if (input) input.checked = true;
  });
});
