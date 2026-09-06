// SPDX-License-Identifier: MIT
export default {
  content: [ "./index.html", "./src/**/*.{js,jsx}" ],
  theme: {
    extend: {
      animation: {
        fadeIn: "fadeIn 0.4s ease-in-out",
        shimmer: "shimmer 2s infinite linear",
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
      },
    },
  },
  plugins: [ require( "@tailwindcss/typography" ) ],
};