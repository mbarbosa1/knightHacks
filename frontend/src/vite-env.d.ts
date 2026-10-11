/// <reference types="vite/client" />

//  Only VITE_* reach the browser.
interface ImportMetaEnv {
  readonly VITE_SIMULATOR_URL?: string;
}
