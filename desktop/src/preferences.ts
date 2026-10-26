import { useEffect, useState } from 'react';

export type Theme = 'system' | 'light' | 'dark';
export type Palette = 'forest' | 'ocean' | 'blossom' | 'graphite';
export type ServerSort = 'recent' | 'name';
interface Preferences { theme: Theme; palette: Palette; favorites: string[]; sort: ServerSort }
const key = 'gaoji.workspace.v1';

function read(): Preferences {
  try {
    const value = JSON.parse(localStorage.getItem(key) || '{}');
    return {
      theme: ['system', 'light', 'dark'].includes(value?.theme) ? value.theme : 'system',
      palette: ['forest', 'ocean', 'blossom', 'graphite'].includes(value?.palette) ? value.palette : 'forest',
      sort: value?.sort === 'name' ? 'name' : 'recent',
      favorites: Array.isArray(value?.favorites) ? value.favorites.filter((id: unknown) => typeof id === 'string').slice(0, 1000) : [],
    };
  } catch { return { theme: 'system', palette: 'forest', sort: 'recent', favorites: [] }; }
}

export function usePreferences() {
  const [preferences, setPreferences] = useState(read);
  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(preferences)); } catch { /* Preferences can remain session-only when storage is unavailable. */ }
    const media = matchMedia('(prefers-color-scheme: dark)');
    document.documentElement.dataset.palette = preferences.palette;
    const apply = () => { document.documentElement.dataset.theme = preferences.theme === 'system' ? media.matches ? 'dark' : 'light' : preferences.theme; };
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, [preferences]);
  return { preferences, setPreferences };
}
