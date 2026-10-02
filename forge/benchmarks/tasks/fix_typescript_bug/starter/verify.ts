import { multiply } from './app.ts';
if (multiply(3, 4) !== 12 || multiply(-2, 5) !== -10) {
  throw new Error('multiply must return a product');
}

