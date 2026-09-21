const form = document.querySelector('#form');
const error = document.querySelector('#error');
const ssoButton = document.querySelector('#ssoLogin');
const ssoDivider = document.querySelector('#ssoDivider');
const guestButton = document.querySelector('#guestLogin');
const guestDivider = document.querySelector('#guestDivider');

fetch('/api/auth/config').then(response => response.json()).then(config => {
  const sso = Boolean(config.sso_login);
  form.hidden = sso;
  ssoButton.hidden = !sso;
  ssoDivider.hidden = !sso;
  guestButton.hidden = !config.guest_enabled;
  guestDivider.hidden = !config.guest_enabled;
}).catch(() => {
  error.textContent = '无法读取登录配置，请刷新重试';
});

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

guestButton.addEventListener('click', async () => {
  error.textContent = '';
  guestButton.disabled = true;
  try {
    const response = await fetch('/api/auth/guest', {method: 'POST'});
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || '游客登录失败');
    location.href = payload.redirect || '/';
  } catch (reason) {
    error.textContent = reason.message;
    guestButton.disabled = false;
  }
});
