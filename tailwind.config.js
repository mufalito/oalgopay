/** Tailwind build config — styles are PRECOMPILED into app/static/tailwind.css
 * (committed). Rebuild only when changing styles:
 *   npx tailwindcss@3.4.17 -c tailwind.config.js -i app/static/tailwind.src.css \
 *       -o app/static/tailwind.css --minify
 */
module.exports = {
  content: [
    "app/templates/**/*.html",
    "app/static/*.js",
  ],
  theme: {
    extend: {
      colors: {
        brand: { 50: "#F2F8FC", 100: "#E4EFF9", 200: "#C9E0F2", 300: "#A8CCEC", 400: "#8FBCE3", 500: "#6FA8D9", 600: "#5A93C4", 700: "#4A7AA3", 800: "#3D6484", 900: "#34546D" },
        mint: { 50: "#F1FAF4", 100: "#E3F4E9", 200: "#C7E8D3", 300: "#A8D8B9", 400: "#8FCBA4", 500: "#74B78D", 600: "#5C9E75", 700: "#4B8160", 800: "#3F6A50", 900: "#355844" },
        peach: { 100: "#FBEEEC", 300: "#E8B4AC", 600: "#C07A6E", 700: "#A26458" },
        honey: { 100: "#FCF4E4", 300: "#EBD5A3", 600: "#C2A052", 700: "#A3853E" },
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
