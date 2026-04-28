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
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
