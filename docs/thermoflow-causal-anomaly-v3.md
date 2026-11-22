# ThermoFlow — Causal Diagnostics i Anomaly Detection 3.0

## Cel

Wykrywać powtarzalne zależności między sygnałami instalacji, a nie oceniać każdy czujnik w izolacji. Przykładowa sekwencja do sprawdzenia: start pompy podłogowej, zmiana temperatury powrotu i odpowiedź położenia zaworu 4D.

## Dane i metoda

- Wykorzystywany jest istniejący poller eModul i zsynchronizowany czas pobrania.
- Bufor jest ulotny, ograniczony do 241 próbek i nie tworzy nowego archiwum.
- Analizowane są stany pomp podłogowej, CWU, zaworu 4D i cyrkulacyjnej oraz temperatura powrotu, CO, spaliny i otwarcie zaworu.
- Start pompy jest parowany z bezpośrednio sąsiednimi próbkami przed/po zdarzeniu, jeśli odstęp nie przekracza 75 sekund.
- Powtarzalny wzorzec wymaga co najmniej trzech sparowanych startów, trzech spadków powrotu o minimum 1°C i mediany zmian nie większej niż -1°C.
- Równoczesna zmiana innej pompy jest pokazywana jako czynnik zakłócający i obniża poziom pewności.
- Dla zdarzeń z wystarczającą historią raportowany jest czas do pierwszej zmiany otwarcia zaworu w oknie 5 min oraz zmiana temperatury kotła po 15 min. To kontekst, nie próg sterowania ani automatyczna ocena zapotrzebowania cieplnego.
- Brak danych, nieaktualność, duplikaty czasu lub luka w historii zamykają analizę; dane nie są interpolowane.

## Interpretacja

Anomaly Detection 3.0 publikuje wzorzec do sprawdzenia, nie alarm bezpieczeństwa. Causal Diagnostics prezentuje hipotezę, liczbę zdarzeń, medianę zmian, kontekst zaworu i czynniki zakłócające. „Umiarkowana” pewność opisuje powtarzalność obserwacji bez wykrytej równoczesnej zmiany innej pompy; nie jest prawdopodobieństwem przyczynowości.

Korelacja czasowa nie dowodzi, że pompa spowodowała spadek temperatury. Brak pomiaru przepływu i pełnego stanu wszystkich odbiorników pozostaje ograniczeniem.

## Bezpieczeństwo

Read-only/advisory. Brak sterowania, zmian nastaw, poleceń dla pomp i zaworu, zmian prognozy paliwa, zapisu do ThingSpeak lub treningu online. Ochrona powrotu pozostaje 55°C ±1°C. Wartość 54°C jest sygnałem do przeglądu, nie potwierdzeniem zadziałania ochrony.

## API i dashboard

Dwa dodatkowe moduły live są publikowane przez istniejący endpoint `GET /api/thermoflow/v1/intelligence`:
- `modules.anomaly_v3`
- `modules.causal_diagnostics`

Oba wyniki dzielą czas ważności istniejącego shadow snapshotu i znikają wraz z nim po wygaśnięciu danych. Widok Intelligence pokazuje pokrycie, status danych, wzorce, liczbę zdarzeń, medianę, poziom pewności i ograniczenia.
