import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './theme.css';
import './style.css';
import './workspace.css';
import './appearance.css';
import './theme-art.css';

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
