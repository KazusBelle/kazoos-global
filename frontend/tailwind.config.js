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
        accent: "#ff6a00",
        discount: "#34d399",
        premium: "#f87171",
        eq: "#e5e7eb",
        ote: "#facc15",
      },
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "monospace"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
      },
    },
  },
  plugins: [],
};
