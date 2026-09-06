// SPDX-License-Identifier: MIT
// pptParamParser.js
// Simple regex-based parameter extraction for PPT generation
// No LLM needed - deterministic parsing

const WORD_NUMBERS = {
  one: 1, two: 2, three: 3, four: 4, five: 5, 
  six: 6, seven: 7, eight: 8
};

const MIN_SLIDES = 1;
const MAX_SLIDES = 8;

const THEMES = ['general', 'swift', 'modern', 'standard'];
const TONES = ['professional', 'educational', 'casual', 'sales_pitch', 'funny'];
const LANGUAGES = ['english', 'hindi', 'tamil', 'telugu', 'kannada', 'malayalam', 'bengali', 'gujarati'];

/**
 * Parse slide count from text: "8", "5 slides", "eight slides", "3", "1"
 */
export function parseSlideCount(text) {
  if (!text) return null;
  const lower = text.toLowerCase();
  
  // Try to find a number: "8", "5 slides", "3 slides"
  const numMatch = text.match(/\b(\d+)\b/);
  if (numMatch) {
    const n = parseInt(numMatch[1], 10);
    // Validate it's in acceptable range (1-8)
    if (n >= MIN_SLIDES && n <= MAX_SLIDES) return n;
  }
  
  // Try word numbers: "five", "eight", "three"
  for (const [word, val] of Object.entries(WORD_NUMBERS)) {
    if (lower.includes(word)) return val;
  }
  
  return null;
}

/**
 * Parse theme from text: "swift", "general", etc.
 */
export function parseTheme(text) {
  if (!text) return null;
  const lower = text.toLowerCase();
  
  for (const theme of THEMES) {
    if (lower.includes(theme)) return theme;
  }
  
  return null;
}

/**
 * Parse tone from text: "professional", "educational", etc.
 */
export function parseTone(text) {
  if (!text) return null;
  const lower = text.toLowerCase();
  
  // Map common variations
  if (lower.includes('sales') || lower.includes('pitch')) return 'sales_pitch';
  if (lower.includes('fun') || lower.includes('casual') || lower.includes('informal')) return 'funny';
  
  for (const tone of TONES) {
    if (lower.includes(tone.replace('_', ' ')) || lower.includes(tone.replace('_', ''))) return tone;
  }
  
  return null;
}

/**
 * Parse language from text
 */
export function parseLanguage(text) {
  if (!text) return null;
  const lower = text.toLowerCase();
  
  for (const lang of LANGUAGES) {
    if (lower.includes(lang)) return lang.charAt(0).toUpperCase() + lang.slice(1);
  }
  
  return null;
}

/**
 * Parse yes/no response
 */
export function parseYesNo(text) {
  if (!text) return null;
  const lower = text.toLowerCase();
  
  const yesPatterns = /\b(yes|yeah|yep|sure|ok|okay|include|with|true|y)\b/;
  const noPatterns = /\b(no|nah|nope|skip|without|don'?t|false|n)\b/;
  
  if (yesPatterns.test(lower)) return true;
  if (noPatterns.test(lower)) return false;
  
  return null;
}

/**
 * Parse export format
 */
export function parseFormat(text) {
  if (!text) return 'pptx';
  const lower = text.toLowerCase();
  
  if (lower.includes('pdf')) return 'pdf';
  if (lower.includes('pptx') || lower.includes('powerpoint') || lower.includes('ppt')) return 'pptx';
  
  return 'pptx'; // default
}

/**
 * Check if user wants to cancel
 */
export function isCancelRequest(text) {
  if (!text) return false;
  const lower = text.toLowerCase();
  
  const cancelPatterns = /\b(cancel|stop|nevermind|never mind|abort|quit|exit|end)\b/;
  return cancelPatterns.test(lower);
}

/**
 * Extract ALL params from initial message
 * Example: "Make a 10 slide ppt about onboarding in Swift theme, professional tone"
 */
export function extractAllParams(text) {
  return {
    n_slides: parseSlideCount(text),
    theme: parseTheme(text),
    tone: parseTone(text),
    language: parseLanguage(text),
    include_table_of_contents: parseYesNo(text),
    export_as: parseFormat(text),
  };
}

/**
 * Get default value for a parameter
 */
export function getDefaultParam(paramKey) {
  const defaults = {
    n_slides: 8,
    theme: 'general',
    tone: 'professional',
    language: 'English',
    include_table_of_contents: false,
    export_as: 'pptx',
  };
  return defaults[paramKey];
}

/**
 * Format param for display
 */
export function formatParamForDisplay(paramKey, value) {
  if (value === null || value === undefined) return 'Not set';
  
  switch (paramKey) {
    case 'n_slides':
      return `${value} slides`;
    case 'theme':
      return value.charAt(0).toUpperCase() + value.slice(1);
    case 'tone':
      return value.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    case 'language':
      return value;
    case 'include_table_of_contents':
      return value ? 'Yes' : 'No';
    case 'export_as':
      return value.toUpperCase();
    default:
      return String(value);
  }
}
