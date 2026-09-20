const form = document.querySelector('#form');
const error = document.querySelector('#error');
form.addEventListener('submit', async event => {
  event.preventDefault();
  error.textContent = '';
  const data = Object.fromEntries(new FormData(form));
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || '登录失败');
    location.href = '/';
  } catch (reason) {
    error.textContent = reason.message;
  }
});
