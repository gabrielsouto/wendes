# Como obter o FIREBASE_REFRESH_TOKEN (uma única vez)

O moderated.jitsi.net exige login (Google/GitHub/Facebook) para abrir a sala.
O bot não faz login interativo — ele reutiliza o *refresh token* do Firebase
gerado por um login manual seu. Esse token não expira sozinho (só se for
revogado), e o bot troca por um JWT novo automaticamente a cada daily.

## Passo a passo (no SEU computador, uma vez)

1. Abra o navegador e acesse: https://moderated.jitsi.net/MinhaSalaDaily
2. Faça o sign-in (Google) quando a página pedir. Você será redirecionado para a sala.
3. Ainda na aba da sala (ou na página de sign-in), abra o DevTools (F12) →
   aba **Application** (Chrome) ou **Armazenamento** (Firefox).
4. No painel esquerdo: **IndexedDB → firebaseLocalStorageDb → firebaseLocalStorage**.
5. Clique no registro cuja chave começa com `firebase:authUser:`.
6. Dentro do valor, localize `stsTokenManager.refreshToken` e copie o valor
   (uma string longa começando geralmente com `AMf-` ou similar).
   - Alternativa via console (F12 → Console) na página logada:
     ```js
     indexedDB.open('firebaseLocalStorageDb').onsuccess = e => {
       const db = e.target.result;
       db.transaction('firebaseLocalStorage').objectStore('firebaseLocalStorage')
         .getAll().onsuccess = ev => console.log(
           ev.target.result[0]?.value?.stsTokenManager?.refreshToken);
     };
     ```
7. No servidor, copie `.env.example` para `.env` e cole o token:
   ```
   FIREBASE_REFRESH_TOKEN=<valor copiado>
   ```
8. Valide com: `.venv/bin/wendes check` — deve mostrar
   "token Firebase renovado com sucesso".

> Importante: o token dá acesso de moderador às suas salas do
> moderated.jitsi.net — trate o `.env` como segredo (não commitar).
