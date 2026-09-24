import { useState } from 'react';

interface LoginFormProps {
  onLogin: (pin: string) => void;
  error: string;
  loading: boolean;
}

export default function LoginForm({ onLogin, error, loading }: LoginFormProps) {
  const [pin, setPin] = useState('');

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    onLogin(pin);
  };

  return (
    <form onSubmit={handleSubmit}>
      <label className="block text-[11px] font-bold text-rose-950 uppercase tracking-[0.6px] mb-[7px]">
        Enter Your Access PIN
      </label>
      <input
        type="password"
        value={pin}
        onChange={(event) => setPin(event.target.value)}
        placeholder="••••"
        autoFocus
        autoComplete="off"
        className="w-full px-[14px] py-[13px] border-2 border-rose-800 rounded-[9px] text-lg text-center tracking-[6px] bg-[#FCF8F8] focus:outline-none focus:border-[#962538] focus:bg-white"
      />
      <p className="text-stone-400 text-[11px] text-center mt-[7px]">
        PIN provided by Circle Office
      </p>
      {error && <p className="text-red-600 text-sm text-center mt-[14px]">{error}</p>}
      <button
        type="submit"
        disabled={loading}
        className="w-full mt-[18px] px-4 py-[14px] bg-amber-600 hover:bg-amber-700 disabled:opacity-70 text-white text-[15px] font-bold rounded-[9px] transition-colors"
        style={{ boxShadow: 'rgba(201,123,30,0.3) 0px 6px 16px' }}
      >
        {loading ? 'Logging in...' : 'Login'}
      </button>
    </form>
  );
}