
#include "AGVDisplayShelf.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "AGVGrabbableObject.h"
#include "Engine/World.h"

AAGVDisplayShelf::AAGVDisplayShelf()
{
    PrimaryActorTick.bCanEverTick = false;

    // 1. 루트 컴포넌트 생성
    ShelfRoot = CreateDefaultSubobject<USceneComponent>(
        TEXT("ShelfRoot")
    );

    RootComponent = ShelfRoot;

    // 2. 완성형 선반 메시 컴포넌트 생성
    ShelfMesh = CreateDefaultSubobject<UStaticMeshComponent>(
        TEXT("ShelfMesh")
    );

    ShelfMesh->SetupAttachment(ShelfRoot);

    // Static Mesh 에셋은 블루프린트에서 지정
    ShelfMesh->SetCollisionEnabled(
        ECollisionEnabled::QueryAndPhysics
    );

    // 3. 상품 적재 슬롯 6개 생성
    for (int32 i = 0; i < 6; i++)
    {
        const FName SlotName(
            *FString::Printf(TEXT("Slot_%d"), i)
        );

        USceneComponent* Slot =
            CreateDefaultSubobject<USceneComponent>(SlotName);

        Slot->SetupAttachment(ShelfRoot);

        // 아래층 0~2, 위층 3~5
        const int32 Column = i % 3;
        const int32 Row = i / 3;

        const float Y = -75.0f + Column * 75.0f;
        const float Z = (Row == 0) ? 45.0f : 107.5f;

        Slot->SetRelativeLocation(
            FVector(-5.0f, Y, Z)
        );

        Slots.Add(Slot);
    }

    // 4. 슬롯 점유 상태 초기화
    OccupiedSlots.Init(nullptr, Slots.Num());
}

FVector AAGVDisplayShelf::GetSlotWorldLocation(
    int32 SlotIndex
) const
{
    if (Slots.IsValidIndex(SlotIndex) &&
        IsValid(Slots[SlotIndex]))
    {
        return Slots[SlotIndex]->GetComponentLocation();
    }

    return GetActorLocation();
}


void AAGVDisplayShelf::BeginPlay()
{
    Super::BeginPlay();

    // 슬롯 수만큼 초기화
    OccupiedSlots.Init(nullptr, Slots.Num());

    if (!InitialProductClass)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("%s: InitialProductClass is not set"),
            *GetName());
        return;
    }

    for (int32 SlotIndex : InitialOccupiedSlots)
    {
        if (!Slots.IsValidIndex(SlotIndex))
        {
            UE_LOG(LogTemp, Warning,
                TEXT("%s: Invalid slot index %d"),
                *GetName(), SlotIndex);
            continue;
        }

        // 중복 슬롯 방지
        if (IsSlotOccupied(SlotIndex))
        {
            continue;
        }

        const FVector SpawnLocation =
            Slots[SlotIndex]->GetComponentLocation();

        const FRotator SpawnRotation =
            GetActorRotation();

        FActorSpawnParameters SpawnParams;
        SpawnParams.SpawnCollisionHandlingOverride =
            ESpawnActorCollisionHandlingMethod::AlwaysSpawn;

        AAGVGrabbableObject* Product =
            GetWorld()->SpawnActor<AAGVGrabbableObject>(
                InitialProductClass,
                SpawnLocation,
                SpawnRotation,
                SpawnParams
            );

        if (Product)
        {
            OccupiedSlots[SlotIndex] = Product;

            UE_LOG(LogTemp, Log,
                TEXT("%s: Product spawned at slot %d"),
                *GetName(), SlotIndex);
        }
    }
}

bool AAGVDisplayShelf::IsSlotOccupied(int32 SlotIndex) const
{
    return OccupiedSlots.IsValidIndex(SlotIndex)
        && IsValid(OccupiedSlots[SlotIndex]);
}

int32 AAGVDisplayShelf::FindProductSlot(
    const AAGVGrabbableObject* Product
) const
{
    if (!IsValid(Product))
    {
        return INDEX_NONE;
    }

    for (int32 i = 0; i < OccupiedSlots.Num(); i++)
    {
        if (OccupiedSlots[i] == Product)
        {
            return i;
        }
    }

    return INDEX_NONE;
}

bool AAGVDisplayShelf::RemoveProductFromSlot(
    AAGVGrabbableObject* Product
)
{
    const int32 SlotIndex = FindProductSlot(Product);

    if (SlotIndex == INDEX_NONE)
    {
        return false;
    }

    OccupiedSlots[SlotIndex] = nullptr;

    UE_LOG(
        LogTemp,
        Log,
        TEXT("%s: Slot %d is now EMPTY"),
        *GetName(),
        SlotIndex
    );

    return true;
}

int32 AAGVDisplayShelf::FindNearestSlot(
    const FVector& WorldLocation
) const
{
    int32 NearestIndex = INDEX_NONE;
    float NearestDistance = TNumericLimits<float>::Max();

    for (int32 i = 0; i < Slots.Num(); i++)
    {
        if (!IsValid(Slots[i]))
        {
            continue;
        }

        const float Distance = FVector::DistSquared(
            WorldLocation,
            Slots[i]->GetComponentLocation()
        );

        if (Distance < NearestDistance)
        {
            NearestDistance = Distance;
            NearestIndex = i;
        }
    }

    return NearestIndex;
}

bool AAGVDisplayShelf::PlaceProductAtSlot(
    AAGVGrabbableObject* Product,
    int32 SlotIndex
)
{
    if (!IsValid(Product) ||
        !Slots.IsValidIndex(SlotIndex) ||
        !IsValid(Slots[SlotIndex]))
    {
        return false;
    }

    // 상품 종류가 다르면 거부
    if (Product->GetProductType() != ShelfProductType)
    {
        return false;
    }

    // 이미 상품이 있으면 거부
    if (IsSlotOccupied(SlotIndex))
    {
        return false;
    }

    Product->Release();

    UStaticMeshComponent* Mesh =
        Product->FindComponentByClass<UStaticMeshComponent>();

    if (Mesh)
    {
        Mesh->SetSimulatePhysics(false);
        Mesh->SetEnableGravity(false);
    }

    Product->SetActorLocationAndRotation(
        Slots[SlotIndex]->GetComponentLocation(),
        GetActorRotation(),
        false,
        nullptr,
        ETeleportType::TeleportPhysics
    );

    OccupiedSlots[SlotIndex] = Product;

    UE_LOG(LogTemp, Log,
        TEXT("%s: Product placed at slot %d"),
        *GetName(), SlotIndex);

    return true;
}

int32 AAGVDisplayShelf::FindFirstEmptySlot(
    EProductType ProductType
) const
{
    // 선반이 취급하는 상품 종류 확인
    if (ShelfProductType != ProductType)
    {
        return INDEX_NONE;
    }

    for (int32 i = 0; i < Slots.Num(); i++)
    {
        if (!IsValid(Slots[i]))
        {
            continue;
        }

        // 비어 있는 슬롯 반환
        if (!IsSlotOccupied(i))
        {
            return i;
        }
    }

    // 빈 슬롯이 없는 경우
    return INDEX_NONE;
}