/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#070709",
        panel: "#0d0e12",
        border: "#1a1c22",
        muted: "#5a5f6b",
        accent: "#E3822D",
        discount: "#529e79",
        premium: "#752727",
        eq: "#529e79",
        ote: "#529e79",
      },
      fontFamily: {
        // Single-family override: Spectrum Sans (self-hosted variable
        // font, see index.css @font-face) for the whole UI. font-mono
        // still resolves so existing utility-class usages don't break;
        // it just points at the same sans, so table number columns lose
        // tabular alignment — deliberate global-look trade.
        mono: ["Spectrum Sans", "ui-sans-serif", "system-ui", "sans-serif"],
        sans: ["Spectrum Sans", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
