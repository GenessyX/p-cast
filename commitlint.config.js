//comitling.config.js at the root of your repo
const types = [
  "ci",
  "feat",
  "fix",
  "chore",
  "bugfix",
  "hotfix",
  "perf",
  "refactor",
  "revert",
  "style",
  "test",
  "docs",
];

module.exports = {
  extends: ["@commitlint/config-angular"],
  rules: {
    "type-enum": [2, "always", types],
  },
};
