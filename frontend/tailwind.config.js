export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // The repo's only brand asset: the resume template accent.
        accent: { DEFAULT: '#5B9BD5', dark: '#3E7BB0' },
      },
    },
  },
}
